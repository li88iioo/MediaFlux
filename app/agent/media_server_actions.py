"""Media Agent 的媒体服务器版本与兼容槽位诊断。"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from app.agent.config_actions import test_media_server
from app.agent.errors import AgentToolError
from app.agent.models import Evidence, ToolResult

_SLOT_ORDER = ("jellyfin", "emby")
_SLOT_LABELS = {
    "jellyfin": "Jellyfin 12",
    "emby": "Emby / Jellyfin 10.x",
}
_VERSION_RE = re.compile(r"^(?P<major>0|[1-9]\d{0,2})(?:\.\d{1,4}){0,3}$")
_CONNECTION_REASONS = {
    "disabled": "server_disabled",
    "not_configured": "configuration_incomplete",
    "redirect_not_allowed": "redirect_blocked",
    "timeout": "request_timeout",
    "connection": "connection_failed",
    "authentication": "authentication_failed",
    "not_found": "system_info_not_found",
    "http_error": "upstream_http_error",
    "invalid_response": "invalid_system_info_response",
    "unavailable": "unexpected_upstream_failure",
}
_DIAGNOSTIC_SEMAPHORE = threading.BoundedSemaphore(2)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def media_server_diagnosis_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if arguments:
        raise AgentToolError("config.diagnose_media_servers 不接受参数")
    return {}


def _safe_version(value: Any) -> tuple[str | None, int | None]:
    version = str(value or "").strip()
    matched = _VERSION_RE.fullmatch(version)
    if not matched:
        return None, None
    return version, int(matched.group("major"))


def _safe_product(value: Any) -> str:
    product = str(value or "").strip().casefold()
    if product == "jellyfin":
        return "jellyfin"
    if product == "emby":
        return "emby"
    return "unknown"


def _compatibility(slot: str, product: str, major: int | None) -> tuple[str, str]:
    if product == "unknown":
        return "review", "product_unrecognized"
    if slot == "jellyfin":
        if product == "emby":
            return "wrong_slot", "use_legacy_slot"
        if major is None:
            return "review", "version_unrecognized"
        if major >= 12:
            return "compatible", "jellyfin12_slot_compatible"
        if major <= 10:
            return "wrong_slot", "use_legacy_slot"
        return "review", "jellyfin_major_not_classified"

    if product == "emby":
        return "compatible", "emby_legacy_slot_compatible"
    if major is None:
        return "review", "version_unrecognized"
    if major == 10:
        return "compatible", "jellyfin10_legacy_slot_compatible"
    if major >= 12:
        return "wrong_slot", "use_jellyfin12_slot"
    return "review", "jellyfin_major_not_classified"


def _node_payload(slot: str, result: ToolResult) -> dict[str, Any]:
    connection_status = str(result.status or "unavailable")
    enabled = connection_status != "disabled"
    configured = connection_status not in {"disabled", "not_configured"}
    node: dict[str, Any] = {
        "slot": slot,
        "slot_label": _SLOT_LABELS[slot],
        "enabled": enabled,
        "configured": configured,
        "online": connection_status == "success",
        "connection_status": connection_status,
        "product": "unknown",
        "version": None,
        "major_version": None,
        "compatibility": "disabled" if not enabled else "unavailable",
        "reason_code": _CONNECTION_REASONS.get(
            connection_status, "unexpected_upstream_failure"
        ),
        "latency_ms": None,
    }
    if connection_status != "success":
        if connection_status == "not_configured":
            node["compatibility"] = "not_configured"
        return node

    product = (
        _safe_product(result.data.get("product"))
        if result.data.get("product_detected") is True
        else "unknown"
    )
    version, major = _safe_version(result.data.get("version"))
    compatibility, reason_code = _compatibility(slot, product, major)
    raw_latency = result.data.get("latency_ms")
    latency_ms = (
        raw_latency
        if isinstance(raw_latency, int) and not isinstance(raw_latency, bool)
        else None
    )
    if latency_ms is not None:
        latency_ms = max(0, min(latency_ms, 11_500))
    node.update(
        {
            "product": product,
            "version": version,
            "major_version": major,
            "compatibility": compatibility,
            "reason_code": reason_code,
            "latency_ms": latency_ms,
        }
    )
    return node


def _probe_all() -> list[ToolResult]:
    with ThreadPoolExecutor(
        max_workers=2, thread_name_prefix="agent-media-server"
    ) as pool:
        futures = [
            pool.submit(test_media_server, {"server_type": slot})
            for slot in _SLOT_ORDER
        ]
        return [future.result() for future in futures]


def diagnose_media_servers(arguments: dict[str, Any]) -> ToolResult:
    del arguments
    if not _DIAGNOSTIC_SEMAPHORE.acquire(timeout=0.5):
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="媒体服务器兼容性诊断正忙，请稍后重试",
            data={
                "probe_mode": "configured_endpoints",
                "network_accessed": False,
                "counts": {
                    "enabled": 0,
                    "configured": 0,
                    "online": 0,
                    "compatible": 0,
                    "attention": 0,
                },
                "nodes": [],
            },
            evidence=[
                Evidence(
                    "configured_media_servers",
                    "未执行远程探测；诊断并发额度暂时已满。",
                    _now(),
                )
            ],
            suggestions=["请稍后重新运行媒体服务器兼容性诊断。"],
            error="媒体服务器兼容性诊断当前繁忙。",
        )

    try:
        results = _probe_all()
    except Exception:
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="暂时无法完成媒体服务器兼容性诊断",
            data={
                "probe_mode": "configured_endpoints",
                "network_accessed": True,
                "counts": {
                    "enabled": 0,
                    "configured": 0,
                    "online": 0,
                    "compatible": 0,
                    "attention": 0,
                },
                "nodes": [],
            },
            evidence=[
                Evidence(
                    "configured_media_servers",
                    "已尝试读取当前媒体服务器配置并执行固定系统信息探测；未返回地址、名称或访问凭据。",
                    _now(),
                )
            ],
            suggestions=["请稍后重试，或先分别测试 Jellyfin 与兼容节点连接。"],
            error="媒体服务器兼容性诊断当前不可用。",
        )
    finally:
        _DIAGNOSTIC_SEMAPHORE.release()

    nodes = [_node_payload(slot, result) for slot, result in zip(_SLOT_ORDER, results)]
    enabled = sum(bool(node["enabled"]) for node in nodes)
    configured = sum(bool(node["configured"]) for node in nodes)
    online = sum(bool(node["online"]) for node in nodes)
    compatible = sum(node["compatibility"] == "compatible" for node in nodes)
    attention = sum(
        node["enabled"] and node["compatibility"] != "compatible" for node in nodes
    )
    network_accessed = any(
        node["connection_status"] not in {"disabled", "not_configured"}
        for node in nodes
    )

    if enabled == 0:
        status = "not_configured"
        ok = True
        summary = "尚未启用媒体服务器"
    elif online == 0 and configured > 0:
        status = "unavailable"
        ok = False
        summary = "已配置的媒体服务器当前均不可用"
    elif attention:
        status = "attention"
        ok = True
        summary = f"媒体服务器诊断完成，{attention} 个节点需要关注"
    else:
        status = "healthy"
        ok = True
        summary = f"媒体服务器兼容性正常，{compatible} 个节点在线"

    reasons = {node["reason_code"] for node in nodes if node["enabled"]}
    suggestions: list[str] = []
    if "configuration_incomplete" in reasons:
        suggestions.append("请在设置中补全已启用节点的地址与访问凭据。")
    if "use_legacy_slot" in reasons:
        suggestions.append(
            "Emby 或 Jellyfin 10.x 应配置在 Emby / Jellyfin 10.x 兼容节点。"
        )
    if "use_jellyfin12_slot" in reasons:
        suggestions.append("Jellyfin 12 及以上版本应配置在 Jellyfin 12 节点。")
    if reasons & {
        "request_timeout",
        "connection_failed",
        "authentication_failed",
        "system_info_not_found",
        "upstream_http_error",
        "invalid_system_info_response",
        "unexpected_upstream_failure",
        "redirect_blocked",
    }:
        suggestions.append("请检查媒体服务器运行状态、鉴权和反向代理路径后重试。")
    if reasons & {
        "version_unrecognized",
        "product_unrecognized",
        "jellyfin_major_not_classified",
    }:
        suggestions.append(
            "当前版本无法自动归类，请核对产品类型与主版本后选择正确节点。"
        )
    if enabled == 0:
        suggestions.append("可在设置中启用 Jellyfin 12 或 Emby / Jellyfin 10.x 节点。")

    return ToolResult(
        ok=ok,
        status=status,
        summary=summary,
        data={
            "probe_mode": "configured_endpoints",
            "network_accessed": network_accessed,
            "counts": {
                "enabled": enabled,
                "configured": configured,
                "online": online,
                "compatible": compatible,
                "attention": attention,
            },
            "nodes": nodes,
        },
        evidence=[
            Evidence(
                "configured_media_servers",
                "使用服务端当前生效配置探测固定系统信息接口；仅返回产品类别、受限版本号和兼容性结论。",
                _now(),
            )
        ],
        suggestions=suggestions,
        error="" if ok else summary,
    )
