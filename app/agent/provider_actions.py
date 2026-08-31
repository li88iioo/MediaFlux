"""统一 Provider Gateway 的 Agent 工具入口。"""
from __future__ import annotations

import re
import threading
from typing import Any

from app.agent.models import ToolContext, ToolResult
from app.agent.provider_gateway import ProviderGateway
from app.agent.provider_models import ProviderGatewayError
from app.agent.provider_operations import build_provider_catalog
from app.agent.providers.media_server import MediaServerProviderTransport
from app.agent.providers.qbittorrent import QBittorrentProviderTransport
from app.agent.registry import AgentToolError

_GATEWAY: ProviderGateway | None = None
_LOCK = threading.Lock()


def get_provider_gateway() -> ProviderGateway:
    global _GATEWAY
    with _LOCK:
        if _GATEWAY is None:
            _GATEWAY = ProviderGateway(
                catalog=build_provider_catalog(),
                transports=[
                    MediaServerProviderTransport(),
                    QBittorrentProviderTransport(),
                ],
            )
        return _GATEWAY


def reset_provider_gateway_for_tests() -> None:
    global _GATEWAY
    with _LOCK:
        if _GATEWAY is not None:
            _GATEWAY.artifacts.clear()
        _GATEWAY = None


def provider_capabilities_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("Provider 能力参数必须是对象")
    allowed = {"provider", "intent", "limit"}
    if set(arguments) - allowed:
        raise AgentToolError("Provider 能力查询包含未开放参数")
    provider = str(arguments.get("provider") or "").strip().casefold()
    if provider not in {"", "media", "qbittorrent"}:
        raise AgentToolError("provider 仅支持 media 或 qbittorrent")
    intent = str(arguments.get("intent") or "").strip()
    if len(intent) > 160:
        raise AgentToolError("intent 最长 160 个字符")
    try:
        limit = int(arguments.get("limit", 12))
    except (TypeError, ValueError, OverflowError) as exc:
        raise AgentToolError("limit 必须是整数") from exc
    if not 1 <= limit <= 24:
        raise AgentToolError("limit 必须在 1 到 24 之间")
    return {"provider": provider, "intent": intent, "limit": limit}


def provider_query_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or set(arguments) != {
        "profile_ref", "operation", "arguments"
    }:
        raise AgentToolError(
            "Provider 查询只接受 profile_ref、operation 和 arguments"
        )
    profile_ref = str(arguments.get("profile_ref") or "").strip()
    operation = str(arguments.get("operation") or "").strip().casefold()
    payload = arguments.get("arguments")
    if not profile_ref or len(profile_ref) > 80:
        raise AgentToolError("profile_ref 无效")
    if not operation or len(operation) > 96:
        raise AgentToolError("operation 无效")
    if not isinstance(payload, dict):
        raise AgentToolError("arguments 必须是对象")
    return {
        "profile_ref": profile_ref,
        "operation": operation,
        "arguments": dict(payload),
    }


def list_provider_capabilities(arguments: dict[str, Any]) -> ToolResult:
    normalized = provider_capabilities_arguments(arguments)
    return get_provider_gateway().capabilities(**normalized)


def query_provider(
    arguments: dict[str, Any], context: ToolContext
) -> ToolResult:
    normalized = provider_query_arguments(arguments)
    try:
        return get_provider_gateway().query(context=context, **normalized)
    except ProviderGatewayError as exc:
        raise AgentToolError(exc.safe_message, code=exc.code) from exc


def provider_plan_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """写计划预览沿用查询入口的静态 operation 参数封装。"""
    return provider_query_arguments(arguments)


def provider_plan_ref_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or set(arguments) != {"plan_ref"}:
        raise AgentToolError("Provider 写计划操作只接受 plan_ref")
    plan_ref = str(arguments.get("plan_ref") or "").strip().upper()
    if not re.fullmatch(r"PP-[0-9A-F]{24}", plan_ref):
        raise AgentToolError("plan_ref 无效")
    return {"plan_ref": plan_ref}


def preview_provider_change(
    arguments: dict[str, Any], context: ToolContext
) -> ToolResult:
    normalized = provider_plan_arguments(arguments)
    try:
        return get_provider_gateway().preview_change(context=context, **normalized)
    except ProviderGatewayError as exc:
        raise AgentToolError(exc.safe_message, code=exc.code) from exc


def prepare_provider_change_execution(
    arguments: dict[str, Any], context: ToolContext
) -> tuple[ToolResult, str]:
    normalized = provider_plan_ref_arguments(arguments)
    try:
        return get_provider_gateway().prepare_change_execution(
            context=context, **normalized
        )
    except ProviderGatewayError as exc:
        raise AgentToolError(exc.safe_message, code=exc.code) from exc


def execute_provider_change_confirmed(
    arguments: dict[str, Any], expected_context: str, context: ToolContext
) -> ToolResult:
    normalized = provider_plan_ref_arguments(arguments)
    try:
        return get_provider_gateway().execute_change(
            context=context,
            expected_context=expected_context,
            **normalized,
        )
    except ProviderGatewayError as exc:
        raise AgentToolError(exc.safe_message, code=exc.code) from exc


def provider_change_status(
    arguments: dict[str, Any], context: ToolContext
) -> ToolResult:
    normalized = provider_plan_ref_arguments(arguments)
    try:
        return get_provider_gateway().change_status(context=context, **normalized)
    except ProviderGatewayError as exc:
        raise AgentToolError(exc.safe_message, code=exc.code) from exc
