"""Media Agent 的多站资源搜索与确认提交动作。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import unicodedata
from datetime import datetime
from typing import Any

from app import config
from app.agent.async_bridge import (
    AsyncBridgeUnavailable,
    ensure_sync_bridge_available,
)
from app.agent.models import Evidence, ToolResult
from app.agent.registry import AgentToolError
from app.clients.guangya import GuangYaClient
from app.indexers.downloads import (
    DownloadRequestCreationError,
    InvalidDownloadData,
    download_result,
    download_result_public,
)
from app.indexers.errors import IndexerError, IndexerValidationError
from app.indexers.models import IndexerMediaSearchRequest
from app.indexers.runtime import get_indexer_service, run_indexer_awaitable_sync

_SITE_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
_RESULT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_TARGETS = frozenset({"qb", "guangya", "both"})


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
    """在访问上游前确认指定站点属于当前启用集合。"""
    if not sites or not config.get_bool("INDEXER_SEARCH_ENABLED", True):
        return
    service = get_indexer_service()
    enabled = set(getattr(service, "enabled_site_ids", ()))
    invalid = [site_id for site_id in sites if site_id not in enabled]
    if invalid:
        raise AgentToolError(f"站点未启用或不存在：{', '.join(invalid)}")


def search_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    _reject_extra(
        arguments,
        {
            "title",
            "original_title",
            "english_title",
            "aliases",
            "year",
            "media_type",
            "page",
            "sites",
            "limit",
        },
    )
    title = _visible_text(arguments.get("title"), name="title", required=True)
    original_title = _visible_text(arguments.get("original_title", ""), name="original_title")
    english_title = _visible_text(arguments.get("english_title", ""), name="english_title")

    raw_aliases = arguments.get("aliases", [])
    if not isinstance(raw_aliases, list) or len(raw_aliases) > 8:
        raise AgentToolError("aliases 必须是最多包含 8 项的数组")
    aliases = [_visible_text(value, name="alias", required=True) for value in raw_aliases]

    year = arguments.get("year")
    if year is not None and (
        isinstance(year, bool)
        or not isinstance(year, int)
        or not 1800 <= year <= 2200
    ):
        raise AgentToolError("year 必须是 1800 到 2200 的整数")
    media_type = arguments.get("media_type", "")
    if (
        not isinstance(media_type, str)
        or media_type.strip().lower() not in {"", "movie", "tv", "anime"}
    ):
        raise AgentToolError("media_type 仅支持 movie、tv 或 anime")
    media_type = media_type.strip().lower()

    page = arguments.get("page", 1)
    if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 100:
        raise AgentToolError("page 必须是 1 到 100 的整数")
    limit = arguments.get("limit", 20)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise AgentToolError("limit 必须是 1 到 50 的整数")

    sites = normalize_search_sites(arguments.get("sites", []))

    try:
        request = IndexerMediaSearchRequest.create(
            title=title,
            original_title=original_title,
            english_title=english_title,
            aliases=aliases,
            year=year,
            media_type=media_type,
            page=page,
        )
    except IndexerValidationError as exc:
        raise AgentToolError(exc.public_message) from exc

    validate_enabled_search_sites(sites)
    return {
        "title": request.title,
        "original_title": request.original_title,
        "english_title": request.english_title,
        "aliases": list(request.aliases),
        "year": request.year,
        "media_type": request.media_type,
        "page": request.page,
        "sites": sites,
        "limit": limit,
    }


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
    if not config.get_bool("INDEXER_SEARCH_ENABLED", True):
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

    request = IndexerMediaSearchRequest.create(
        title=arguments["title"],
        original_title=arguments["original_title"],
        english_title=arguments["english_title"],
        aliases=arguments["aliases"],
        year=arguments["year"],
        media_type=arguments["media_type"],
        page=arguments["page"],
    )
    service = get_indexer_service()
    search_awaitable = service.search_media(request, arguments["sites"] or None)
    bounded_timeout = None if timeout_seconds is None else float(timeout_seconds)
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
    except IndexerError as exc:
        return ToolResult(False, exc.code, exc.public_message, error=exc.public_message)

    items = [_public_item(item) for item in result.items[:arguments["limit"]]]
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
        summary += "，部分站点暂不可用"
    elif not items:
        summary = "已完成多站搜索，暂未找到匹配资源"
    return ToolResult(
        ok=ok,
        status=status,
        summary=summary,
        data={
            "query": _safe_text(result.query, 120),
            "page": int(result.page),
            "items": items,
            "sites_attempted": [
                _safe_text(value, 32) for value in result.sites_attempted[:16]
            ],
            "sites_succeeded": [
                _safe_text(value, 32) for value in result.sites_succeeded[:16]
            ],
            "errors": errors,
            "partial": bool(result.partial),
            "cached": bool(result.cached),
            "has_more": bool(result.has_more),
        },
        evidence=[
            Evidence(
                "indexer_service",
                (
                    "服务器端多站索引已完成；成功 "
                    f"{len(result.sites_succeeded)}/{len(result.sites_attempted)} 个站点。"
                ),
                _now(),
            )
        ],
        suggestions=(
            ["回复“第 2 个到 qB / 光鸭 / 两边”，即可进入提交确认。"]
            if items
            else []
        ),
    )


def _target_readiness(target: str) -> dict[str, bool]:
    readiness: dict[str, bool] = {}
    if target in {"qb", "both"}:
        readiness["qb"] = bool(str(config.get("QB_URL", "") or "").strip())
    if target in {"guangya", "both"}:
        try:
            readiness["guangya"] = bool(GuangYaClient().logged_in)
        except Exception:
            readiness["guangya"] = False
    return readiness


def _stored_resource(arguments: dict[str, str]):
    service = get_indexer_service()
    item = service.result_store.get(arguments["result_id"])
    enabled = set(getattr(service, "enabled_site_ids", ()))
    if item.site_id not in enabled:
        raise IndexerValidationError(
            "stored result provider is disabled",
            public_message="资源来源当前未启用",
        )
    if item.download_state not in {"ready", "resolvable"} or not item.download_kinds:
        raise IndexerValidationError("stored result is not downloadable", public_message="该资源当前不可下载")
    return service, item


def preview_submit_resource(arguments: dict[str, str]) -> ToolResult:
    if not config.get_bool("INDEXER_SEARCH_ENABLED", True):
        return ToolResult(False, "disabled", "资源检索功能已关闭", error="资源检索功能未启用。")
    try:
        _service, item = _stored_resource(arguments)
    except IndexerError as exc:
        return ToolResult(False, exc.code, exc.public_message, error=exc.public_message)

    readiness = _target_readiness(arguments["target"])
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
                "download_kinds": [
                    kind
                    for kind in item.download_kinds
                    if kind in {"magnet", "torrent"}
                ],
            },
            "target": arguments["target"],
            "backends": readiness,
            "effects": ["服务端解析短期资源结果", "创建下载请求", "向所选下载后端提交任务"],
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


def submit_confirmation_context(arguments: dict[str, str]) -> str:
    if not config.get_bool("INDEXER_SEARCH_ENABLED", True):
        raise IndexerValidationError("indexer disabled")
    _service, item = _stored_resource(arguments)
    payload = {
        "result_id": arguments["result_id"],
        "target": arguments["target"],
        "site_id": item.site_id,
        "title": item.title,
        "download_state": item.download_state,
        "download_kinds": sorted(item.download_kinds),
        "backends": _target_readiness(arguments["target"]),
        "enabled": True,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def submit_resource(arguments: dict[str, str]) -> ToolResult:
    if not config.get_bool("INDEXER_SEARCH_ENABLED", True):
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
    service = get_indexer_service()
    try:
        result = run_indexer_awaitable_sync(
            download_result(
                service,
                arguments["result_id"],
                arguments["target"],
                origin_namespace="agent",
            )
        )
    except IndexerError as exc:
        return ToolResult(False, "conflict", exc.public_message, error=exc.public_message)
    except InvalidDownloadData:
        return ToolResult(False, "unavailable", "资源下载数据无效", error="资源下载数据无效。")
    except DownloadRequestCreationError:
        return ToolResult(False, "unavailable", "下载请求创建失败", error="下载请求创建失败。")
    except Exception:
        return ToolResult(False, "unavailable", "下载处理失败", error="下载处理失败，请稍后重试。")

    public = {
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
    if result.get("duplicate"):
        return ToolResult(
            False,
            "conflict",
            "该资源已经提交或正在处理中",
            data=public,
            error="请勿重复提交。",
        )
    if result.get("status") == "manual_review":
        return ToolResult(
            False,
            "review_required",
            "下载任务提交结果待核对",
            data=public,
            error="请先核对下载器，勿直接重复提交。",
        )
    if not result.get("ok"):
        return ToolResult(
            False,
            "unavailable",
            "下载任务提交失败",
            data=public,
            error="所选下载后端未接受任务。",
        )
    return ToolResult(
        True,
        "accepted",
        "下载任务已提交",
        data=public,
        evidence=[
            Evidence(
                "download_dispatcher",
                "已通过服务器端资源解析与下载分发器提交；响应未包含磁力、种子内容、路径或凭据。",
                _now(),
            )
        ],
        suggestions=["可前往下载任务页查看进度。"],
    )

def submit_batch_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    _reject_extra(arguments, {"result_ids", "target"})
    raw_ids = arguments.get("result_ids")
    if not isinstance(raw_ids, list) or not 2 <= len(raw_ids) <= 12:
        raise AgentToolError("result_ids 必须包含 2 到 12 个资源")
    result_ids: list[str] = []
    for raw in raw_ids:
        if not isinstance(raw, str) or not _RESULT_ID_RE.fullmatch(raw.strip()):
            raise AgentToolError("result_id 无效")
        value = raw.strip()
        if value not in result_ids:
            result_ids.append(value)
    if len(result_ids) < 2:
        raise AgentToolError("批量提交至少需要 2 个不同资源")
    target = str(arguments.get("target") or "").strip().lower()
    if target not in _TARGETS:
        raise AgentToolError("target 仅支持 qb、guangya 或 both")
    return {"result_ids": result_ids, "target": target}


def preview_submit_resource_batch(arguments: dict[str, Any]) -> ToolResult:
    if not config.get_bool("INDEXER_SEARCH_ENABLED", True):
        return ToolResult(False, "disabled", "资源检索功能已关闭", error="资源检索功能未启用。")
    resources: list[dict[str, Any]] = []
    try:
        for result_id in arguments["result_ids"]:
            _service, item = _stored_resource({
                "result_id": result_id,
                "target": arguments["target"],
            })
            resources.append({
                "position": len(resources) + 1,
                "site_name": _safe_text(item.site_name, 80),
                "title": _safe_text(item.title, 300),
                "download_state": item.download_state,
            })
    except IndexerError as exc:
        return ToolResult(False, exc.code, exc.public_message, error=exc.public_message)
    readiness = _target_readiness(arguments["target"])
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
        evidence=[Evidence(
            "indexer_result_store",
            "已逐项校验短期资源标识和下载后端就绪状态；未返回磁力、路径或凭据。",
            _now(),
        )],
        suggestions=["请核对批量资源标题和统一下载目标后再确认。"],
    )


def submit_batch_confirmation_context(arguments: dict[str, Any]) -> str:
    payload: list[dict[str, Any]] = []
    if not config.get_bool("INDEXER_SEARCH_ENABLED", True):
        raise IndexerValidationError("indexer disabled")
    for result_id in arguments["result_ids"]:
        _service, item = _stored_resource({
            "result_id": result_id,
            "target": arguments["target"],
        })
        payload.append({
            "result_id": result_id,
            "site_id": item.site_id,
            "title": item.title,
            "download_state": item.download_state,
            "download_kinds": sorted(item.download_kinds),
        })
    context = {
        "resources": payload,
        "target": arguments["target"],
        "backends": _target_readiness(arguments["target"]),
        "enabled": True,
    }
    return hashlib.sha256(json.dumps(
        context, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def submit_resource_batch(arguments: dict[str, Any]) -> ToolResult:
    if not config.get_bool("INDEXER_SEARCH_ENABLED", True):
        return ToolResult(False, "conflict", "资源检索功能已关闭", error="相关配置已变化，请重新搜索。")
    try:
        ensure_sync_bridge_available()
    except AsyncBridgeUnavailable:
        return ToolResult(False, "unavailable", "批量资源提交当前调用上下文不可用", error="请从同步 Agent 查询入口提交资源。")
    service = get_indexer_service()

    async def submit_all() -> list[dict[str, Any]]:
        return list(await asyncio.gather(*(
            download_result_public(
                service,
                result_id,
                arguments["target"],
                origin_namespace="agent",
            )
            for result_id in arguments["result_ids"]
        )))

    try:
        items = run_indexer_awaitable_sync(submit_all())
    except Exception:
        return ToolResult(False, "unavailable", "批量下载处理失败", error="下载处理失败，请稍后重试。")
    counts = {
        status: sum(item.get("status") == status for item in items)
        for status in ("submitted", "partial", "manual_review", "failed", "duplicate")
    }
    accepted = counts["submitted"] + counts["partial"]
    review_required = counts["manual_review"]
    if accepted and not counts["failed"] and not counts["duplicate"] and not review_required:
        status, summary = "accepted", f"{accepted} 个下载任务已提交"
    elif accepted:
        status, summary = "partial", f"批量提交完成：{accepted} 个已受理，{len(items) - accepted} 个未受理"
    elif review_required:
        status, summary = "review_required", f"{review_required} 个下载任务提交结果待核对"
    elif counts["duplicate"] and not counts["failed"]:
        status, summary = "conflict", "所选资源均已提交或正在处理中"
    else:
        status, summary = "unavailable", "批量资源提交未成功"
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
            "items": items,
        },
        evidence=[Evidence(
            "download_dispatcher",
            "逐项通过服务器端资源解析和下载分发器提交；各项独立幂等且允许部分失败。",
            _now(),
        )],
        suggestions=["可询问：刚才批量下载到哪了。"],
        error=(
            "部分下载任务提交结果待核对，请先核对下载器，勿直接重复提交。"
            if review_required
            else "部分或全部资源未被下载后端接受。" if accepted < len(items)
            else ""
        ),
    )
