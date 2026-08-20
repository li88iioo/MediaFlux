"""Media Agent 的非敏感资源站点配置读取与确认写入。"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import secrets
from typing import Any

from app import config
from app.agent.models import Evidence, ToolResult
from app.agent.registry import AgentToolError
from app.indexers.config import (
    DEFAULT_INDEXER_SITE_IDS,
    INDEXER_SITE_LABELS,
    INDEXER_SITE_ORDER,
    build_indexer_site_updates,
    normalize_indexer_site_ids,
)
from app.logger import get_logger

logger = get_logger(__name__)
_TARGET_KEYS = (
    "INDEXER_SEARCH_ENABLED",
    "INDEXER_ENABLED_SITES",
    "INDEXER_SUKEBEI_ENABLED",
)
_ALLOWED_ARGUMENTS = {"site_ids", "enable_search"}
_RUNTIME_REFRESH_TIMEOUT_SECONDS = 5.0


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def indexer_sites_summary_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if arguments:
        raise AgentToolError(f"不支持的工具参数：{', '.join(sorted(arguments))}")
    return {}


def indexer_sites_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    extra = set(arguments) - _ALLOWED_ARGUMENTS
    if extra:
        raise AgentToolError(f"不支持的工具参数：{', '.join(sorted(extra))}")
    if "site_ids" not in arguments:
        raise AgentToolError("site_ids 为必填项")
    raw_site_ids = arguments["site_ids"]
    if not isinstance(raw_site_ids, list):
        raise AgentToolError("site_ids 必须是字符串数组")
    if len(raw_site_ids) > len(INDEXER_SITE_ORDER):
        raise AgentToolError("资源站点数量超出允许范围")
    try:
        site_ids = normalize_indexer_site_ids(raw_site_ids)
    except ValueError as exc:
        raise AgentToolError(str(exc)) from exc
    if not site_ids:
        raise AgentToolError("至少选择一个资源站点")
    if len(site_ids) != len(raw_site_ids):
        raise AgentToolError("site_ids 不允许重复或空白项")
    normalized = {"site_ids": list(site_ids)}
    if "enable_search" in arguments:
        if not isinstance(arguments["enable_search"], bool):
            raise AgentToolError("enable_search 必须是布尔值")
        normalized["enable_search"] = arguments["enable_search"]
    return normalized


def _requested_updates(arguments: dict[str, Any]) -> dict[str, str]:
    updates = build_indexer_site_updates(arguments["site_ids"])
    if "enable_search" in arguments:
        updates["INDEXER_SEARCH_ENABLED"] = (
            "1" if arguments["enable_search"] else "0"
        )
    return updates


def current_indexer_site_ids(*, strict: bool = False) -> tuple[str, ...]:
    """返回当前白名单站点；增量写入时可要求损坏配置严格失败。"""
    configured = config.get(
        "INDEXER_ENABLED_SITES",
        ",".join(DEFAULT_INDEXER_SITE_IDS),
    )
    raw = str(configured or "")
    try:
        requested = set(normalize_indexer_site_ids(raw))
    except ValueError as exc:
        if strict:
            raise AgentToolError(
                "当前资源站点配置包含未知或无效项，请先在设置页修复",
                code="precondition_failed",
            ) from exc
        requested = {
            part.strip().lower()
            for part in raw.split(",")
            if part.strip().lower() in INDEXER_SITE_ORDER
        }
    if config.get_bool("INDEXER_SUKEBEI_ENABLED", False):
        requested.add("sukebei")
    return tuple(site_id for site_id in INDEXER_SITE_ORDER if site_id in requested)


def _site_projection(site_ids: tuple[str, ...] | list[str]) -> list[dict[str, str]]:
    return [
        {"site_id": site_id, "label": INDEXER_SITE_LABELS[site_id]}
        for site_id in site_ids
    ]


def summarize_indexer_sites(_arguments: dict[str, Any]) -> ToolResult:
    site_ids = current_indexer_site_ids()
    return ToolResult(
        ok=True,
        status="ready" if site_ids else "not_configured",
        summary=(
            f"当前启用了 {len(site_ids)} 个资源站点"
            if site_ids
            else "当前没有启用资源站点"
        ),
        data={
            "site_count": len(site_ids),
            "sites": _site_projection(site_ids),
            "search_enabled": config.get_bool("INDEXER_SEARCH_ENABLED", False),
            "managed_by_environment": any(
                config.has_external_override(key) for key in _TARGET_KEYS
            ),
        },
        evidence=[Evidence(
            "server_configuration",
            "仅返回固定白名单站点的 ID、展示名与数量；未返回其他配置内容。",
            _now(),
        )],
        suggestions=(
            []
            if site_ids
            else ["可明确指定至少一个资源站点并在预检后确认保存。"]
        ),
    )


def _capture(arguments: dict[str, Any]) -> dict[str, Any]:
    snapshot, values = config.read_env_snapshot(config.ENV_FILE)
    requested = tuple(arguments["site_ids"])
    current = current_indexer_site_ids()
    return {
        "snapshot": snapshot,
        "snapshot_present": snapshot is not None,
        "snapshot_sha256": hashlib.sha256(snapshot or b"").hexdigest(),
        "persisted_search": values.get("INDEXER_SEARCH_ENABLED", "<unset>"),
        "persisted_sites": values.get("INDEXER_ENABLED_SITES", "<unset>"),
        "persisted_sukebei": values.get("INDEXER_SUKEBEI_ENABLED", "<unset>"),
        "current_search_enabled": config.get_bool("INDEXER_SEARCH_ENABLED", False),
        "current_site_ids": current,
        "requested_site_ids": requested,
        "requested_enable_search": arguments.get("enable_search"),
        "external_overrides": tuple(
            key for key in _TARGET_KEYS if config.has_external_override(key)
        ),
    }


def _fingerprint(state: dict[str, Any]) -> str:
    payload = {
        "snapshot_present": state["snapshot_present"],
        "snapshot_sha256": state["snapshot_sha256"],
        "persisted_search": state["persisted_search"],
        "persisted_sites": state["persisted_sites"],
        "persisted_sukebei": state["persisted_sukebei"],
        "current_search_enabled": state["current_search_enabled"],
        "current_site_ids": list(state["current_site_ids"]),
        "requested_site_ids": list(state["requested_site_ids"]),
        "requested_enable_search": state["requested_enable_search"],
        "external_overrides": list(state["external_overrides"]),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _precondition_failure(state: dict[str, Any]) -> ToolResult | None:
    if state["external_overrides"]:
        return ToolResult(
            ok=False,
            status="environment_override",
            summary="资源站点选择由运行环境管理，Agent 不会覆盖部署配置",
            error="目标配置由运行环境覆盖。",
            suggestions=["请在部署环境中调整资源站点配置后重启服务。"],
        )
    requested_search = state["requested_enable_search"]
    search_unchanged = (
        requested_search is None
        or state["current_search_enabled"] is requested_search
    )
    if state["current_site_ids"] == state["requested_site_ids"] and search_unchanged:
        return ToolResult(
            ok=False,
            status="no_changes",
            summary="资源站点已经是目标选择",
            error="资源站点选择没有变化。",
        )
    return None


def preview_set_indexer_sites(arguments: dict[str, Any]) -> ToolResult:
    state = _capture(arguments)
    failed = _precondition_failure(state)
    if failed is not None:
        return failed
    requested = state["requested_site_ids"]
    current = state["current_site_ids"]
    enables_sensitive_site = "sukebei" in requested and "sukebei" not in current
    effects = [
        "保存固定白名单内的资源站点选择",
        "同步兼容站点开关",
        "重建当前进程的多站资源搜索服务",
    ]
    if state["requested_enable_search"] is True:
        effects.insert(0, "开启多站资源索引")
    elif state["requested_enable_search"] is False:
        effects.insert(0, "关闭多站资源索引")
    if enables_sensitive_site:
        effects.insert(0, "将启用成人内容站点 Sukebei；该站点默认关闭")
    return ToolResult(
        ok=True,
        status="confirmation_required",
        summary=(
            f"确认后将启用 {len(requested)} 个资源站点（包含 Sukebei）"
            if enables_sensitive_site
            else f"确认后将启用 {len(requested)} 个资源站点"
        ),
        data={
            "current_count": len(current),
            "requested_count": len(requested),
            "current_sites": _site_projection(current),
            "requested_sites": _site_projection(requested),
            "search_enabled": (
                state["current_search_enabled"]
                if state["requested_enable_search"] is None
                else state["requested_enable_search"]
            ),
            "requested_enable_search": state["requested_enable_search"],
            "effects": effects,
        },
        evidence=[Evidence(
            "server_configuration",
            "仅比较固定白名单站点选择与配置快照；未返回其他配置值。",
            _now(),
        )],
    )


def indexer_sites_confirmation_context(arguments: dict[str, Any]) -> str:
    return _fingerprint(_capture(arguments))


def _refresh_runtime() -> dict[str, bool]:
    status = {"web": True, "telegram": True}
    try:
        from app.indexers.runtime import (
            run_indexer_awaitable_sync,
            shutdown_indexer_service,
        )

        run_indexer_awaitable_sync(
            shutdown_indexer_service(),
            timeout_seconds=_RUNTIME_REFRESH_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        status["web"] = False
        logger.warning(
            "Agent Web 资源站点运行时刷新失败 type=%s", type(exc).__name__
        )
    try:
        from app.modules.telegram_resource_search import shutdown_telegram_indexer_worker

        if not shutdown_telegram_indexer_worker(
            timeout=_RUNTIME_REFRESH_TIMEOUT_SECONDS
        ):
            status["telegram"] = False
            logger.warning("Agent Telegram 资源站点运行时刷新超时")
    except Exception as exc:
        status["telegram"] = False
        logger.warning(
            "Agent Telegram 资源站点运行时刷新失败 type=%s", type(exc).__name__
        )
    return status


def set_indexer_sites_confirmed(
    arguments: dict[str, Any], expected_context: str
) -> ToolResult:
    state = _capture(arguments)
    if not secrets.compare_digest(_fingerprint(state), str(expected_context or "")):
        return ToolResult(
            ok=False,
            status="conflict",
            summary="配置已被其他操作修改，请重新预检",
            error="配置已变化。",
        )
    failed = _precondition_failure(state)
    if failed is not None:
        return failed

    updates = _requested_updates(arguments)
    try:
        config.update_runtime_env_file(
            config.ENV_FILE,
            updates,
            expected=state["snapshot"],
        )
    except config.ConcurrentConfigUpdateError:
        return ToolResult(
            ok=False,
            status="conflict",
            summary="配置已被其他操作修改，请重新预检",
            error="配置已变化。",
        )
    except config.ExternalConfigOverrideError:
        return ToolResult(
            ok=False,
            status="environment_override",
            summary="资源站点选择由运行环境管理，Agent 未修改配置",
            error="目标配置由运行环境覆盖。",
        )
    except (config.AtomicPublishError, OSError, ValueError):
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="资源站点选择暂时无法保存",
            error="配置写入失败，请稍后重试。",
        )

    runtime_refresh = _refresh_runtime()
    runtime_refreshed = all(runtime_refresh.values())
    suggestions: list[str] = []
    if not runtime_refresh["web"]:
        suggestions.append("Web 搜索服务未能及时刷新；请重启当前服务以确保应用新站点。")
    if not runtime_refresh["telegram"]:
        suggestions.append(
            "Telegram 搜索 worker 未能及时停止；请重启 Telegram Bot 或当前服务以应用新站点。"
        )
    site_ids = tuple(arguments["site_ids"])
    return ToolResult(
        ok=True,
        status="completed",
        summary=(
            f"已开启多站资源索引，并保存 {len(site_ids)} 个资源站点"
            if arguments.get("enable_search") is True
            else f"已保存 {len(site_ids)} 个资源站点"
        ),
        data={
            "site_count": len(site_ids),
            "search_enabled": config.get_bool("INDEXER_SEARCH_ENABLED", False),
            "sites": _site_projection(site_ids),
            "runtime_refreshed": runtime_refreshed,
            "runtime_refresh": runtime_refresh,
            "runtime_scope": "current_process",
        },
        evidence=[Evidence(
            "server_configuration",
            "使用一次性确认票据与配置快照原子更新固定白名单站点选择。",
            _now(),
        )],
        suggestions=suggestions,
    )


def verify_indexer_sites_write(
    arguments: dict[str, Any],
    result: ToolResult,
) -> ToolResult:
    """回读资源站点白名单的持久化结果，不暴露内部配置内容。"""
    expected = _requested_updates(arguments)
    _snapshot, values = config.read_env_snapshot(config.ENV_FILE)
    verified = all(values.get(key) == value for key, value in expected.items())
    data = dict(result.data)
    data["verification_state"] = "verified" if verified else "pending"
    suggestions = list(result.suggestions)
    evidence = list(result.evidence)
    if verified:
        evidence.append(Evidence(
            "server_configuration",
            "已从服务端持久化配置快照回读资源站点选择；未返回配置键或配置值。",
            _now(),
        ))
    else:
        message = "站点选择已提交，但持久化回读尚未确认完整结果；请刷新设置页复核。"
        if message not in suggestions:
            suggestions.append(message)
    return ToolResult(
        ok=True,
        status=result.status,
        summary=result.summary,
        data=data,
        evidence=evidence,
        suggestions=suggestions,
        error="",
    )


def set_indexer_sites(arguments: dict[str, Any]) -> ToolResult:
    """防御性占位；该写工具只能通过确认后的 confirmed_handler 执行。"""
    del arguments
    raise AgentToolError("该工具需要确认，不能直接执行", code="confirmation_required")
