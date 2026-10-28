"""Media Agent 的多站资源搜索与确认提交动作。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import unicodedata
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from app import config
from app.agent.async_bridge import (
    AsyncBridgeUnavailable,
    ensure_sync_bridge_available,
)
from app.agent.domains.resources.candidate_context import (
    MAX_RESOURCE_CANDIDATES,
    attach_resource_candidate_reference,
)
from app.agent.errors import AgentToolError
from app.agent.models import Evidence, ToolResult
from app.clients.guangya import GuangYaClient, close_guangya_client
from app.clients.http import HttpError
from app.indexers.config import AIPAN_URL, DYGANG_URL, YS5266_URL
from app.indexers.downloads import (
    DownloadRequestCreationError,
    InvalidDownloadData,
    download_indexer_result,
    download_indexer_result_public,
    indexer_download_destination,
)
from app.indexers.errors import IndexerResultExpired, IndexerValidationError
from app.indexers.models import IndexerMediaSearchRequest
from app.indexers.runtime import get_indexer_service, run_indexer_awaitable_sync
from app.modules.download_dispatcher import public_dispatch_summary
from app.modules.media_probe import infer_dynamic_ranges, infer_media_profile

_SITE_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
_RESULT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_TARGETS = frozenset({"qb", "guangya", "both"})
_SORT_MODES = frozenset(
    {
        "published_desc",
        "relevance_desc",
        "episode_desc",
        "seeders_desc",
        "size_desc",
        "size_asc",
    }
)
_SPLIT_SITE_HOSTS = frozenset(
    host
    for base in (AIPAN_URL, DYGANG_URL, YS5266_URL)
    for hostname in (urlsplit(base).hostname,)
    if hostname is not None
    for host in (hostname, hostname.removeprefix("www."))
)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _reject_extra(arguments: dict[str, Any], allowed: set[str]) -> None:
    extra = set(arguments) - allowed
    if extra:
        raise AgentToolError(f"不支持的工具参数：{', '.join(sorted(extra))}")


def _visible_text(
    value: Any,
    *,
    name: str,
    required: bool = False,
    maximum: int = 120,
) -> str:
    if not isinstance(value, str):
        raise AgentToolError(f"{name} 必须是字符串")
    text = unicodedata.normalize("NFKC", value).strip()
    if required and not text:
        raise AgentToolError(f"{name} 不能为空")
    has_control = any(unicodedata.category(char).startswith("C") for char in text)
    if len(text) > maximum or has_control:
        message = (
            f"{name} 必须是 1 到 {maximum} 个可见字符"
            if required
            else f"{name} 最多 {maximum} 个可见字符"
        )
        raise AgentToolError(message)
    return text


def normalize_search_sites(raw_sites: Any) -> list[str]:
    """规范化并预检资源搜索站点，供复合 Agent 工具复用。"""
    if not isinstance(raw_sites, list) or len(raw_sites) > 16:
        raise AgentToolError("sites 必须是最多包含 16 项的数组")
    sites: list[str] = []
    for raw_site in raw_sites:
        if not isinstance(raw_site, str):
            raise AgentToolError("站点 ID 必须是字符串")
        site_id = raw_site.strip().lower()
        if not _SITE_ID_RE.fullmatch(site_id):
            raise AgentToolError("站点 ID 无效")
        if site_id not in sites:
            sites.append(site_id)

    return sites


def validate_enabled_search_sites(sites: list[str]) -> None:
    """在访问上游前确认指定站点属于 Agent 当前允许集合。"""
    if not sites or not config.get_bool("INDEXER_SEARCH_ENABLED"):
        return
    service = get_indexer_service()
    enabled = set(service.site_ids_for_scope("agent"))
    invalid = [site_id for site_id in sites if site_id not in enabled]
    if invalid:
        if invalid == ["sukebei"]:
            raise AgentToolError(
                "Sukebei 尚未显式授权；请单独授权成人来源后重试",
                code="precondition_failed",
            )
        raise AgentToolError(f"站点未启用或不存在：{', '.join(invalid)}")


TECHNICAL_FILTER_CHOICES = {
    "resolution": ("", "2160p", "1080p", "720p", "480p"),
    "dynamic_range": ("", "SDR", "HDR", "HDR10", "HDR10+", "DoVi", "HLG"),
    "exclude_hdr": (False, True),
}


def technical_filter_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """搜索和既有候选筛选使用相同规格；省略/空值不限，不继承别次搜索条件。"""
    quality = {key: arguments[key] for key in TECHNICAL_FILTER_CHOICES if key in arguments}
    for key, value in quality.items():
        choices = TECHNICAL_FILTER_CHOICES[key]
        if type(value) is not type(choices[0]) or value not in choices:
            raise AgentToolError(f"{key} 不在允许范围内")
    return quality


def filter_technical_candidates(
    items: list[dict[str, Any]], arguments: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """仅使用可见发布名证据，未知与冲突分开统计；保留原候选身份/序号。"""
    requirements = {key: value for key, value in technical_filter_arguments(arguments).items() if value}
    matched, unknown, mismatched = [], 0, 0
    for item in items:
        title = _safe_text(item.get("title"), 300)
        profile = infer_media_profile(title) if requirements else None
        observed = {key: getattr(profile, key) for key in requirements if key != "exclude_hdr"}
        ranges = infer_dynamic_ranges(title) if requirements.get("exclude_hdr") or requirements.get("dynamic_range") else ()
        if requirements.get("exclude_hdr"):
            observed["exclude_hdr"] = not any(value != "SDR" for value in ranges)
        if desired := requirements.get("dynamic_range"):
            matches = (ranges == ("SDR",) if desired == "SDR" else
                       any(value != "SDR" for value in ranges) if desired == "HDR" else desired in ranges)
            observed["dynamic_range"] = desired if matches else "mismatch" if ranges else ""
        if any(value != requirements[key] for key, value in observed.items() if value != ""):
            mismatched += 1
        elif any(not value for value in observed.values()):
            unknown += 1
        else:
            matched.append(item)
    return matched, {"requested": requirements, "basis": "release_title_only", "considered": len(items),
                     "scope": "input_candidate_titles",
                     "unknown_excluded": unknown, "mismatched_excluded": mismatched,
                     "stream_verified": False}


def search_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """新搜索规范化全部条件；显式引用续查只规范化覆盖字段，不注入默认值。"""
    defaults: dict[str, Any] = {
        "title": "", "original_title": "", "english_title": "", "aliases": [],
        "year": None, "media_type": "", "season": None, "episode": None,
        "page": 1, "sort_mode": "relevance_desc", "sites": [], "limit": 20,
        "resolution": "", "dynamic_range": "", "exclude_hdr": False,
    }
    _reject_extra(arguments, {*defaults, "resource_candidates_ref"})
    reference = arguments.get("resource_candidates_ref")
    if "resource_candidates_ref" in arguments and (
        not isinstance(reference, str) or not re.fullmatch(r"ref_[A-Za-z0-9_-]{16,160}", reference)
    ):
        raise AgentToolError("resource_candidates_ref 不是有效的资源候选引用")
    normalized = {**({} if reference else defaults), **arguments}
    for key in ("title", "original_title", "english_title"):
        if key in normalized:
            normalized[key] = _visible_text(normalized[key], name=key, required=key == "title")
    if "aliases" in normalized:
        raw_aliases = normalized["aliases"]
        if not isinstance(raw_aliases, list) or len(raw_aliases) > 8:
            raise AgentToolError("aliases 必须是最多包含 8 项的数组")
        normalized["aliases"] = [_visible_text(value, name="alias", required=True) for value in raw_aliases]
    for key, minimum, maximum in (
        ("year", 1800, 2200), ("season", 0, 100), ("episode", 1, 1000),
        ("page", 1, 100), ("limit", 1, MAX_RESOURCE_CANDIDATES),
    ):
        if key not in normalized or (normalized[key] is None and key in {"year", "season", "episode"}):
            continue
        value = normalized[key]
        if type(value) is not int or not minimum <= value <= maximum:
            raise AgentToolError(f"{key} 必须是 {minimum} 到 {maximum} 的整数")
    for key, choices in (("media_type", {"", "movie", "tv", "anime"}), ("sort_mode", _SORT_MODES)):
        if key not in normalized:
            continue
        value = normalized[key]
        if not isinstance(value, str) or value.strip().lower() not in choices:
            raise AgentToolError(f"{key} 不在允许范围内")
        normalized[key] = value.strip().lower()
    normalized.update(technical_filter_arguments(normalized))
    if "sites" in normalized:
        normalized["sites"] = normalize_search_sites(normalized["sites"])
        validate_enabled_search_sites(normalized["sites"])
    if not reference:
        try:
            request = IndexerMediaSearchRequest.create(**{
                key: value for key, value in normalized.items()
                if key not in {"sites", "limit", *TECHNICAL_FILTER_CHOICES}
            })
        except IndexerValidationError as exc:
            raise AgentToolError(exc.public_message) from exc
        normalized.update(asdict(request), aliases=list(request.aliases))
    return normalized


def submit_arguments(arguments: dict[str, Any]) -> dict[str, str]:
    _reject_extra(arguments, {"result_id", "target"})
    result_id = arguments.get("result_id")
    if not isinstance(result_id, str) or not _RESULT_ID_RE.fullmatch(result_id.strip()):
        raise AgentToolError("result_id 无效")
    target = arguments.get("target")
    if not isinstance(target, str) or target.strip().lower() not in _TARGETS:
        raise AgentToolError("target 仅支持 qb、guangya 或 both")
    return {"result_id": result_id.strip(), "target": target.strip().lower()}


def _safe_text(value: Any, maximum: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:maximum]


def _safe_nonnegative(value: Any, maximum: int = 10**15) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, min(int(value), maximum))
    except (TypeError, ValueError, OverflowError):
        return None


def _public_item(item) -> dict[str, Any]:
    payload = item.to_public_dict()
    return {
        "result_id": str(payload.get("result_id") or "")[:128],
        "site_id": _safe_text(payload.get("site_id"), 32),
        "site_name": _safe_text(payload.get("site_name"), 80),
        "title": _safe_text(payload.get("title"), 300),
        "category": _safe_text(payload.get("category"), 80),
        "size_text": _safe_text(payload.get("size_text"), 64),
        "size_bytes": _safe_nonnegative(payload.get("size_bytes")),
        "seeders": _safe_nonnegative(payload.get("seeders"), 10**9),
        "leechers": _safe_nonnegative(payload.get("leechers"), 10**9),
        "downloads": _safe_nonnegative(payload.get("downloads"), 10**9),
        "published_at": _safe_text(payload.get("published_at"), 40),
        "download_state": _safe_text(payload.get("download_state"), 24),
        "match_reasons": [_safe_text(reason, 80) for reason in payload.get("match_reasons", [])],
        "download_kinds": [
            kind
            for kind in payload.get("download_kinds", [])
            if kind in {"magnet", "torrent"}
        ],
    }


def search_resources(
    arguments: dict[str, Any],
    *,
    timeout_seconds: float | None = None,
) -> ToolResult:
    if not config.get_bool("INDEXER_SEARCH_ENABLED"):
        return ToolResult(
            ok=False,
            status="disabled",
            summary="多站资源搜索当前已关闭",
            suggestions=["请由管理员在设置中启用资源检索后重试。"],
            error="资源检索功能未启用。",
        )

    try:
        ensure_sync_bridge_available()
    except AsyncBridgeUnavailable:
        return ToolResult(
            False,
            "unavailable",
            "资源检索当前调用上下文不可用",
            error="请从同步 Agent 查询入口调用资源检索。",
        )

    if "resource_candidates" in arguments:
        snapshot = arguments["resource_candidates"]
        base = snapshot.get("_query_arguments") if isinstance(snapshot, dict) else None
        if not isinstance(base, dict):
            raise AgentToolError("此候选引用没有单次查询条件，请提供完整条件重新搜索", code="precondition_failed")
        arguments = search_arguments({
            **base, "page": 1,
            **{key: value for key, value in arguments.items() if key != "resource_candidates"},
        })

    request = IndexerMediaSearchRequest.create(
        title=arguments["title"],
        original_title=arguments["original_title"],
        english_title=arguments["english_title"],
        aliases=arguments["aliases"],
        year=arguments["year"],
        media_type=arguments["media_type"],
        season=arguments.get("season"),
        episode=arguments.get("episode"),
        page=arguments["page"],
        sort_mode=arguments.get("sort_mode", "relevance_desc"),
    )
    service = get_indexer_service()
    bounded_timeout = None if timeout_seconds is None else float(timeout_seconds)
    # 为共享检索的取消收尾留出小预算；外层硬截止仍不延长。
    search_options: dict[str, Any] = {"scope": "agent"}
    if bounded_timeout is not None:
        search_options["timeout_seconds"] = max(0.001, bounded_timeout - min(0.25, bounded_timeout / 10))
    search_awaitable = service.search_media(
        request, arguments["sites"] or None, **search_options,
    )
    try:
        result = run_indexer_awaitable_sync(
            search_awaitable,
            timeout_seconds=bounded_timeout,
        )
    except TimeoutError:
        return ToolResult(
            False,
            "timeout",
            "多站资源搜索达到本次耗时上限",
            error="资源检索超时。",
        )
    except HttpError as exc:
        return ToolResult(False, exc.code, exc.public_message, error=exc.public_message)

    matching_items, technical_filter = filter_technical_candidates(
        [_public_item(item) for item in result.items], arguments,
    )
    items = matching_items[: arguments["limit"]]
    candidate_position = 0
    for item in items:
        if (
            _RESULT_ID_RE.fullmatch(str(item.get("result_id") or ""))
            and str(item.get("download_state") or "").lower() in {"ready", "resolvable"}
            and bool(set(item.get("download_kinds") or []) & {"magnet", "torrent"})
        ):
            candidate_position += 1
            item["position"] = candidate_position
    errors = [
        {
            "site_id": _safe_text(error.site_id, 32),
            "code": _safe_text(error.code, 40),
            "message": _safe_text(error.message, 120),
        }
        for error in result.errors[:16]
    ]
    if result.partial:
        status = "partial"
    elif items:
        status = "success"
    else:
        status = "empty"
    ok = bool(result.sites_succeeded)
    summary = f"找到 {len(items)} 项可查看资源"
    if result.partial:
        summary = (f"找到 {len(items)} 项可查看资源，部分来源未完成" if items
                   else "本轮未找到匹配资源，部分来源未完成，暂不能确认是否无资源")
    elif not items:
        summary = "已完成多站搜索，暂未找到匹配资源"
    truncated = (any(result.site_truncated_counts.values()) or len(matching_items) > len(items)
                 or any(error.code == "coverage_limited" for error in result.errors))
    if truncated:
        summary += "；仅覆盖本次返回范围，未覆盖部分仍未知"
    visible = [item for item in items if "episode_conflict" not in item["match_reasons"]]
    if len(visible) != len(items):
        summary += f"；其中 {len(items) - len(visible)} 项与请求季集冲突，不作为匹配候选"
    tool_result = ToolResult(
        ok=ok,
        status=status,
        summary=summary,
        data={
            "query": _safe_text(result.query, 120),
            "effective_query": dict(arguments),
            "page": (result.page),
            "sort_mode": request.sort_mode,
            "items": items,
            "technical_filter": technical_filter,
            "sites_attempted": [
                _safe_text(value, 32) for value in result.sites_attempted[:16]
            ],
            "sites_succeeded": [
                _safe_text(value, 32) for value in result.sites_succeeded[:16]
            ],
            "errors": errors,
            "partial": (result.partial),
            "cached": (result.cached),
            "has_more": (result.has_more),
            "truncated": truncated,
            "site_collected_counts": dict(result.site_collected_counts),
            "site_truncated_counts": dict(result.site_truncated_counts),
        },
        evidence=[
            Evidence(
                "indexer_service",
                (
                    ("服务器端多站索引已完成；成功 " if result.complete else "本轮检索达到时限，保留已返回结果；已返回 ")
                    + f"{len(result.sites_succeeded)}/{len(result.sites_attempted)} 个站点。"
                ),
                _now(),
            )
        ],
    )
    if len(visible) != len(items):
        tool_result.model_data = {**tool_result.data, "items": visible,
                                  "query_scope": {"basis": "returned_release_titles", "returned": len(items),
                                                  "known_conflicts": len(items) - len(visible),
                                                  "without_known_conflict": len(visible), "global_availability": "unknown"}}
    return attach_resource_candidate_reference(
        tool_result,
        result_store=service.result_store,
        query_arguments=arguments,
    )


def download_target_readiness(target: str) -> dict[str, bool]:
    readiness: dict[str, bool] = {}
    if target in {"qb", "both"}:
        readiness["qb"] = bool((config.get("QB_URL", "") or "").strip())
    if target in {"guangya", "both"}:
        client = None
        try:
            client = GuangYaClient()
            readiness["guangya"] = (client.logged_in)
        except Exception:  # noqa: BLE001 - 后端就绪探测必须安全降级
            readiness["guangya"] = False
        finally:
            close_guangya_client(client)
    return readiness


def _stored_resource(arguments: dict[str, str], *, service: Any | None = None):
    service = service or get_indexer_service()
    item = service.get_result(arguments["result_id"], scope="agent")
    if item.site_id == "btbtla":
        try:
            detail_host = (urlsplit(str(item.detail_url or "")).hostname or "").lower().rstrip(".")
        except ValueError:
            detail_host = ""
        if detail_host in _SPLIT_SITE_HOSTS:
            raise IndexerResultExpired(
                "legacy combined-site result cannot be rebound to an independent site",
                public_message="资源站点已拆分，旧资源确认已过期，请重新搜索后确认。",
            )
    if item.download_state not in {"ready", "resolvable"} or not item.download_kinds:
        raise IndexerValidationError(
            "stored result is not downloadable", public_message="该资源当前不可下载"
        )
    return service, item


def _resource_confirmation_payload(
    arguments: dict[str, str], item: Any, readiness: dict[str, bool], destination: Mapping[str, object]
) -> dict[str, Any]:
    return {
        "result_id": arguments["result_id"],
        "target": arguments["target"],
        "site_id": str(item.site_id),
        "title": str(item.title),
        "download_state": str(item.download_state),
        "download_kinds": sorted(str(kind) for kind in item.download_kinds),
        "backends": readiness,
        "enabled": True,
        "destination": destination,
    }


def _capture_submit_resource(arguments: dict[str, str]) -> dict[str, Any]:
    if not config.get_bool("INDEXER_SEARCH_ENABLED"):
        return {"enabled": False, "fingerprint": "disabled"}
    try:
        service, item = _stored_resource(arguments)
        destination = indexer_download_destination(item, arguments["target"])
    except HttpError as exc:
        return {
            "enabled": True,
            "resource_error": exc,
            "fingerprint": f"resource-error:{exc.code}",
        }
    readiness = download_target_readiness(arguments["target"])
    payload = _resource_confirmation_payload(arguments, item, readiness, destination)
    return {
        "enabled": True,
        "service": service,
        "item": item,
        "readiness": readiness,
        "destination": destination,
        "fingerprint": hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }


def _preview_submit_resource(
    arguments: dict[str, str], state: dict[str, Any]
) -> ToolResult:
    if not state.get("enabled"):
        return ToolResult(
            False,
            "disabled",
            "资源检索功能已关闭",
            error="资源检索功能未启用。",
        )
    resource_error = state.get("resource_error")
    if isinstance(resource_error, HttpError):
        return ToolResult(
            False,
            resource_error.code,
            resource_error.public_message,
            error=resource_error.public_message,
        )

    item = state["item"]
    readiness = dict(state["readiness"])
    unavailable = [name for name, ready in readiness.items() if not ready]
    if unavailable:
        labels = {"qb": "qBittorrent", "guangya": "光鸭"}
        missing = "、".join(labels[name] for name in unavailable)
        return ToolResult(
            False,
            "not_configured",
            f"{missing} 尚未配置或登录",
            data={"target": arguments["target"], "backends": readiness},
            error="所选下载目标尚未就绪。",
        )

    return ToolResult(
        True,
        "confirmation_required",
        "确认后将提交 1 个资源到所选下载目标",
        data={
            "resource": {
                "result_id": arguments["result_id"],
                "site_id": _safe_text(item.site_id, 32),
                "site_name": _safe_text(item.site_name, 80),
                "title": _safe_text(item.title, 300),
                "download_state": item.download_state,
                "receiving_folder": _safe_text(state["destination"].get("gy_target_name", ""), 100),
                "download_kinds": [
                    kind
                    for kind in item.download_kinds
                    if kind in {"magnet", "torrent"}
                ],
            },
            "target": arguments["target"],
            "backends": readiness,
            "effects": [
                "服务端解析短期资源结果",
                "创建下载请求",
                "向所选下载后端提交任务",
            ],
        },
        evidence=[
            Evidence(
                "indexer_result_store",
                "已校验短期资源标识、来源状态和下载后端就绪状态；未返回磁力、种子内容或凭据。",
                _now(),
            )
        ],
        suggestions=["请核对资源标题、来源和下载目标后再确认。"],
    )


def prepare_submit_resource(
    arguments: dict[str, str],
) -> tuple[ToolResult, str]:
    state = _capture_submit_resource(arguments)
    return _preview_submit_resource(arguments, state), str(state["fingerprint"])


def _submit_resource(
    arguments: dict[str, str], *, service: Any | None = None
) -> ToolResult:
    if not config.get_bool("INDEXER_SEARCH_ENABLED"):
        return ToolResult(
            False,
            "conflict",
            "资源检索功能已关闭",
            error="相关配置已变化，请重新搜索。",
        )
    try:
        ensure_sync_bridge_available()
    except AsyncBridgeUnavailable:
        return ToolResult(
            False,
            "unavailable",
            "资源提交当前调用上下文不可用",
            error="请从同步 Agent 查询入口提交资源。",
        )
    service = service or get_indexer_service()
    try:
        result = run_indexer_awaitable_sync(
            download_indexer_result(
                service,
                arguments["result_id"],
                arguments["target"],
                origin_namespace="agent",
                scope="agent",
            )
        )
    except HttpError as exc:
        return ToolResult(
            False, "conflict", exc.public_message, error=exc.public_message
        )
    except InvalidDownloadData:
        return ToolResult(
            False, "unavailable", "资源下载数据无效", error="资源下载数据无效。"
        )
    except DownloadRequestCreationError:
        return ToolResult(
            False, "unavailable", "下载请求创建失败", error="下载请求创建失败。"
        )
    except Exception:  # noqa: BLE001 - Provider 错误统一投影为安全失败
        return ToolResult(
            False, "unavailable", "下载处理失败", error="下载处理失败，请稍后重试。"
        )

    public: dict[str, Any] = {
        key: result.get(key)
        for key in (
            "result_id",
            "request_id",
            "created",
            "target",
            "status",
            "succeeded",
            "failed",
            "duplicate",
        )
    }
    public.update(public_dispatch_summary(result))
    status, summary = {
        "duplicate": ("conflict", "该资源已经提交或正在处理中"),
        "manual_review": ("review_required", "下载任务提交结果待核对"),
        "failed": ("unavailable", "下载任务提交失败"),
        "partial": ("partial", "下载任务仅部分目标已提交"),
        "submitted": ("accepted", "下载任务已提交"),
    }[public["status"]]
    return ToolResult(
        public["ok"], status, summary, data=public,
        evidence=[Evidence(
            "download_dispatcher",
            "已通过服务器端资源解析与下载分发器处理；结果不包含磁力、种子内容、路径或凭据。",
            _now(),
        )],
        suggestions=["可前往下载任务页查看进度；结果未知时先核验，勿直接重复提交。"],
        error=public["error"],
    )


def submit_resource_confirmed(
    arguments: dict[str, str], expected_context: str
) -> ToolResult:
    state = _capture_submit_resource(arguments)
    if not secrets.compare_digest(
        str(state["fingerprint"]), (expected_context or "")
    ):
        raise AgentToolError(
            "资源或下载配置已变化，请重新预检", code="confirmation_stale"
        )
    return _submit_resource(arguments, service=state.get("service"))


def _capture_submit_resource_batch(arguments: dict[str, Any]) -> dict[str, Any]:
    if not config.get_bool("INDEXER_SEARCH_ENABLED"):
        return {"enabled": False, "fingerprint": "disabled"}
    service = get_indexer_service()
    resources: list[dict[str, Any]] = []
    items: list[Any] = []
    try:
        for result_id in arguments["result_ids"]:
            _service, item = _stored_resource(
                {"result_id": result_id, "target": arguments["target"]},
                service=service,
            )
            items.append(item)
            resources.append(
                {
                    "result_id": result_id,
                    "site_id": str(item.site_id),
                    "site_name": str(item.site_name),
                    "title": str(item.title),
                    "download_state": str(item.download_state),
                    "download_kinds": sorted(str(kind) for kind in item.download_kinds),
                    "destination": indexer_download_destination(item, arguments["target"]),
                }
            )
    except HttpError as exc:
        return {
            "enabled": True,
            "resource_error": exc,
            "fingerprint": f"resource-error:{exc.code}",
        }
    readiness = download_target_readiness(arguments["target"])
    payload = {
        "resources": resources,
        "target": arguments["target"],
        "backends": readiness,
        "enabled": True,
    }
    return {
        "enabled": True,
        "service": service,
        "items": items,
        "resources": resources,
        "readiness": readiness,
        "fingerprint": hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }


def _preview_submit_resource_batch(
    arguments: dict[str, Any], state: dict[str, Any]
) -> ToolResult:
    if not state.get("enabled"):
        return ToolResult(
            False,
            "disabled",
            "资源检索功能已关闭",
            error="资源检索功能未启用。",
        )
    resource_error = state.get("resource_error")
    if isinstance(resource_error, HttpError):
        return ToolResult(
            False,
            resource_error.code,
            resource_error.public_message,
            error=resource_error.public_message,
        )
    items = list(state["items"])
    readiness = dict(state["readiness"])
    unavailable = [name for name, ready in readiness.items() if not ready]
    if unavailable:
        labels = {"qb": "qBittorrent", "guangya": "光鸭"}
        return ToolResult(
            False,
            "not_configured",
            f"{'、'.join(labels[name] for name in unavailable)} 尚未配置或登录",
            data={"target": arguments["target"], "backends": readiness},
            error="所选下载目标尚未就绪。",
        )
    resources = [
        {
            "position": position,
            "site_name": _safe_text(item.site_name, 80),
            "title": _safe_text(item.title, 300),
            "download_state": item.download_state,
            "receiving_folder": _safe_text(resource.get("destination", {}).get("gy_target_name", ""), 100),
        }
        for position, (item, resource) in enumerate(zip(items, state["resources"], strict=True), start=1)
    ]
    return ToolResult(
        True,
        "confirmation_required",
        f"确认后将批量提交 {len(resources)} 个资源到所选下载目标",
        data={
            "count": len(resources),
            "resources": resources,
            "target": arguments["target"],
            "backends": readiness,
            "effects": [
                "逐项解析当前会话的短期资源结果",
                "每项独立创建下载请求并保持幂等",
                "部分失败不会回滚已经成功的项目",
            ],
        },
        evidence=[
            Evidence(
                "indexer_result_store",
                "已逐项校验短期资源标识和下载后端就绪状态；未返回磁力、路径或凭据。",
                _now(),
            )
        ],
        suggestions=["请核对批量资源标题和统一下载目标后再确认。"],
    )


def prepare_submit_resource_batch(
    arguments: dict[str, Any],
) -> tuple[ToolResult, str]:
    state = _capture_submit_resource_batch(arguments)
    return _preview_submit_resource_batch(arguments, state), str(state["fingerprint"])


def _converge_batch_duplicates(items: list[dict[str, Any]]) -> int:
    """仅关联同批、同请求、同目标的唯一实际提交结果，不查询全局任务。

    duplicate/ok/created 仍描述本候选未再次提交；batch_result 描述关联的
    提交结果（submitted 并非下载完成），existing_status 不再保留瞬时快照。
    不从结果推断新的重试授权；有多个实际提交结果时不猜测其先后顺序。
    """
    outcomes = {
        "submitted": "已受理（不代表下载完成）",
        "partial": "部分下载目标成功，其余失败",
        "manual_review": "提交结果待核对，勿直接重复提交",
        "failed": "提交失败",
    }
    completed: dict[tuple[int | None, str | None], list[dict[str, Any]]] = {}
    for item in items:
        request_id = item.get("request_id")
        if (
            isinstance(request_id, int)
            and not isinstance(request_id, bool)
            and request_id > 0
            and item.get("target") in _TARGETS
            and not item.get("duplicate")
            and item.get("status") in outcomes
        ):
            completed.setdefault((request_id, item["target"]), []).append(item)

    merged = 0
    for item in items:
        if not item.get("duplicate"):
            continue
        matches = completed.get((item.get("request_id"), item.get("target")), [])
        if len(matches) != 1:
            continue
        final = matches[0]
        item["batch_result"] = {
            key: final.get(key)
            for key in ("result_id", "status", "succeeded", "failed", "error")
        }
        item["existing_status"] = final["status"]
        item["can_resubmit"] = False
        item["resubmit_target"] = ""
        item["error"] = f"已合并到同批下载请求，未新增任务；{outcomes[final['status']]}"
        merged += 1
    return merged


def _submit_resource_batch(
    arguments: dict[str, Any], *, service: Any | None = None
) -> ToolResult:
    if not config.get_bool("INDEXER_SEARCH_ENABLED"):
        return ToolResult(
            False,
            "conflict",
            "资源检索功能已关闭",
            error="相关配置已变化，请重新搜索。",
        )
    try:
        ensure_sync_bridge_available()
    except AsyncBridgeUnavailable:
        return ToolResult(
            False,
            "unavailable",
            "批量资源提交当前调用上下文不可用",
            error="请从同步 Agent 查询入口提交资源。",
        )
    service = service or get_indexer_service()

    async def submit_all() -> list[dict[str, Any]]:
        return list(
            await asyncio.gather(
                *(
                    download_indexer_result_public(
                        service,
                        result_id,
                        arguments["target"],
                        origin_namespace="agent",
                        scope="agent",
                    )
                    for result_id in arguments["result_ids"]
                )
            )
        )

    try:
        items = run_indexer_awaitable_sync(submit_all())
    except Exception:  # noqa: BLE001 - 批量 Provider 错误统一安全投影
        return ToolResult(
            False,
            "unavailable",
            "批量下载处理失败",
            error="下载处理失败，请稍后重试。",
        )
    merged = _converge_batch_duplicates(items)
    counts = {
        status: sum(item.get("status") == status for item in items)
        for status in ("submitted", "partial", "manual_review", "failed", "duplicate")
    }
    accepted = counts["submitted"] + counts["partial"]
    review_required = counts["manual_review"]
    unaccepted = len(items) - merged - accepted
    if (
        accepted
        and not counts["failed"]
        and counts["duplicate"] == merged
        and not counts["partial"]
        and not review_required
    ):
        status, summary = "accepted", f"{accepted} 个下载任务已提交"
    elif accepted:
        status, summary = (
            "partial",
            f"批量提交完成：{accepted} 个已受理，{unaccepted} 个未受理",
        )
    elif review_required:
        status, summary = (
            "review_required",
            f"{review_required} 个下载任务提交结果待核对",
        )
    elif counts["duplicate"] and not counts["failed"]:
        status, summary = "conflict", "所选资源均已提交或正在处理中"
    else:
        status, summary = "unavailable", "批量资源提交未成功"
    if counts["partial"]:
        summary += f"；{counts['partial']} 个任务仅部分下载目标成功"
    if merged:
        summary += f"；{merged} 个同批重复候选已合并（未新增任务）"
    return ToolResult(
        bool(accepted),
        status,
        summary,
        data={
            "target": arguments["target"],
            "total": len(items),
            "succeeded": accepted,
            "review_required": review_required,
            "failed": counts["failed"],
            "duplicate": counts["duplicate"],
            "merged": merged,
            "items": items,
        },
        evidence=[
            Evidence(
                "download_dispatcher",
                "逐项通过服务器端资源解析和下载分发器提交；各项独立幂等且允许部分失败。",
                _now(),
            )
        ],
        suggestions=["可询问：刚才批量下载到哪了。"],
        error=(
            "部分下载任务提交结果待核对，请先核对下载器，勿直接重复提交。"
            if review_required
            else "部分下载目标提交失败。"
            if counts["partial"]
            else "部分或全部资源未被下载后端接受。"
            if unaccepted
            else ""
        ),
    )


def submit_resource_batch_confirmed(
    arguments: dict[str, Any], expected_context: str
) -> ToolResult:
    state = _capture_submit_resource_batch(arguments)
    if not secrets.compare_digest(
        str(state["fingerprint"]), (expected_context or "")
    ):
        raise AgentToolError(
            "资源集合或下载配置已变化，请重新预检", code="confirmation_stale"
        )
    return _submit_resource_batch(arguments, service=state.get("service"))
