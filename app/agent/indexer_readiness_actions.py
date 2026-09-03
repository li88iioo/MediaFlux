"""Media Agent 的多站资源索引器本地就绪诊断。"""
from __future__ import annotations

from datetime import datetime
from typing import Any
import re
import unicodedata

from app import config
from app.agent.models import Evidence, ToolResult
from app.agent.registry import AgentToolError
from app.indexers.runtime import get_indexer_service
from app.logger import get_logger

logger = get_logger(__name__)

_SENSITIVE_SITE_IDS = frozenset({"sukebei"})
_ALLOWED_DOWNLOAD_KINDS = frozenset({"magnet", "torrent"})
_SITE_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def indexer_readiness_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if arguments:
        raise AgentToolError("indexer.diagnose_readiness 不接受参数")
    return {}


def _safe_label(value: Any, *, fallback: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if (
        not text
        or len(text) > 80
        or any(unicodedata.category(char).startswith("C") for char in text)
    ):
        return fallback
    return text


def _empty_data(*, enabled: bool | None) -> dict[str, Any]:
    return {
        "probe_mode": "local",
        "network_accessed": False,
        "filesystem_accessed": False,
        "indexer_enabled": enabled,
        "counts": {
            "registered": 0,
            "enabled": 0,
            "searchable": 0,
            "downloadable": 0,
            "attention": 0,
        },
        "transport": {
            "mode": "direct",
            "outbound_proxy_applied": False,
        },
        "sites": [],
    }


def diagnose_indexer_readiness(_arguments: dict[str, Any]) -> ToolResult:
    try:
        enabled = config.get_bool("INDEXER_SEARCH_ENABLED")
    except Exception as exc:
        logger.warning("Agent 索引器就绪诊断读取开关失败 type=%s", type(exc).__name__)
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="暂时无法读取索引器本地就绪状态",
            data=_empty_data(enabled=None),
            evidence=[
                Evidence(
                    "indexer_local_config",
                    "尝试读取本地索引器总开关；未发起资源站请求。",
                    _now(),
                )
            ],
            suggestions=["请检查索引器本地配置后重试。"],
            error="索引器就绪诊断当前不可用。",
        )
    if not enabled:
        return ToolResult(
            ok=True,
            status="disabled",
            summary="多站资源搜索当前未启用",
            data=_empty_data(enabled=False),
            evidence=[
                Evidence(
                    "indexer_local_config",
                    "读取本地索引器总开关；未创建站点请求、未访问网络或文件系统。",
                    _now(),
                )
            ],
            suggestions=["如需搜索资源站，请在设置中启用多站资源搜索。"],
        )

    try:
        service = get_indexer_service()
        registry = service.registry
        registered_ids = tuple(registry.ids())
        enabled_ids = set(service.enabled_site_ids)
    except Exception as exc:
        logger.warning("Agent 索引器就绪诊断读取注册表失败 type=%s", type(exc).__name__)
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="暂时无法读取索引器本地就绪状态",
            data=_empty_data(enabled=True),
            evidence=[
                Evidence(
                    "indexer_local_registry",
                    "尝试读取本地索引器注册表；未发起资源站请求。",
                    _now(),
                )
            ],
            suggestions=["请检查索引器本地配置后重试。"],
            error="索引器就绪诊断当前不可用。",
        )

    sites: list[dict[str, Any]] = []
    attention = 0
    searchable = 0
    downloadable = 0
    enabled_count = 0

    for raw_site_id in registered_ids:
        site_id = str(raw_site_id or "").strip().lower()
        if not _SITE_ID_RE.fullmatch(site_id):
            attention += 1
            continue
        site_enabled = site_id in enabled_ids
        try:
            adapter = registry.get(site_id)
            capabilities = adapter.capabilities
            kinds = sorted(
                {
                    str(kind).strip().lower()
                    for kind in tuple(getattr(capabilities, "download_kinds", ()) or ())
                    if str(kind).strip().lower() in _ALLOWED_DOWNLOAD_KINDS
                }
            )
            pagination_supported = bool(
                getattr(capabilities, "pagination_supported", False)
            )
            site_name = _safe_label(getattr(adapter, "site_name", ""), fallback=site_id)
            search_available = site_enabled
            download_available = site_enabled and bool(kinds)
            if site_enabled:
                searchable += 1
                if download_available:
                    downloadable += 1
            reason = "ready" if site_enabled else "site_disabled"
            if site_enabled and not kinds:
                reason = "search_only"
        except Exception:
            kinds = []
            pagination_supported = False
            site_name = site_id
            search_available = False
            download_available = False
            reason = "registry_unavailable"
            attention += 1

        if site_enabled:
            enabled_count += 1

        sites.append(
            {
                "site_id": site_id,
                "site_name": site_name,
                "enabled": site_enabled,
                "search_available": search_available,
                "download_available": download_available,
                "download_kinds": kinds,
                "pagination_supported": pagination_supported,
                "sensitive": site_id in _SENSITIVE_SITE_IDS,
                "reason": reason,
            }
        )

    if not enabled_count:
        status = "no_enabled_sites"
        summary = "多站资源搜索已启用，但没有启用站点"
    elif attention:
        status = "attention"
        summary = f"索引器本地配置可用，但有 {attention} 个注册项需要关注"
    else:
        status = "ready"
        summary = f"索引器本地就绪，当前启用 {enabled_count} 个站点"

    suggestions: list[str] = []
    if not enabled_count:
        suggestions.append("请在设置中至少选择一个参与资源检索的站点。")
    if attention:
        suggestions.append("部分站点注册信息不可读取；请检查本地索引器配置后重试。")
    if enabled_count and downloadable == 0:
        suggestions.append("当前启用站点仅支持搜索，无法直接提交下载。")

    return ToolResult(
        ok=True,
        status=status,
        summary=summary,
        data={
            "probe_mode": "local",
            "network_accessed": False,
            "filesystem_accessed": False,
            "indexer_enabled": True,
            "counts": {
                "registered": len(sites),
                "enabled": enabled_count,
                "searchable": searchable,
                "downloadable": downloadable,
                "attention": attention,
            },
            "transport": {
                "mode": "direct",
                "outbound_proxy_applied": False,
            },
            "sites": sites,
        },
        evidence=[
            Evidence(
                "indexer_local_registry",
                "读取本地索引器注册表与能力声明；未搜索资源站、未访问网络或文件系统。",
                _now(),
            )
        ],
        suggestions=suggestions,
    )
