"""媒体追更订阅的安全摘要与单条启停动作。"""
from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
import json
import re
import secrets
from typing import Any
import unicodedata

from app import database as db
from app.agent.async_bridge import AsyncBridgeUnavailable, ensure_sync_bridge_available
from app.agent.models import Evidence, ToolResult
from app.agent.registry import AgentToolError
from app.agent.workspace_actions import _safe_title
from app.discovery.models import MediaCard, ProviderError
from app.discovery.service import get_discovery_service
from app.indexers.runtime import run_indexer_awaitable_sync
from app.logger import get_logger
from app.modules.media_subscriptions import (
    MediaSubscriptionError,
    get_media_subscription_service,
)

logger = get_logger(__name__)
_MAX_SUMMARIES = 16
_MAX_UPDATE_SUBSCRIPTIONS = 8
_MAX_UPDATE_CANDIDATES = 12
_UPDATE_PREVIEW_TIMEOUT_SECONDS = 35.0
_UPDATE_ITEM_TIMEOUT_SECONDS = 25.0
_CREATE_TIMEOUT_SECONDS = 35.0
_ALLOWED_PROVIDERS = frozenset({"tmdb", "douban", "bangumi"})
_ALLOWED_MEDIA_TYPES = frozenset({"movie", "tv"})
_ALLOWED_CHECK_INTERVALS = frozenset({4320, 10080})
_PUBLIC_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,180}$")
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


def media_subscription_summaries_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if arguments:
        raise AgentToolError("media.subscription_summaries 不接受参数")
    return {}


def media_subscription_updates_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if arguments:
        raise AgentToolError("media.subscription_updates 不接受参数")
    return {}


def media_subscription_summary_arguments(arguments: dict[str, Any]) -> dict[str, int]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if set(arguments) != {"subscription_id"}:
        raise AgentToolError("media.get_subscription_summary 只接受 subscription_id 参数")
    return {"subscription_id": _strict_subscription_id(arguments.get("subscription_id"))}


def media_subscription_enabled_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if set(arguments) != {"subscription_id", "enabled"}:
        raise AgentToolError(
            "media.set_subscription_enabled 只接受 subscription_id 和 enabled 参数"
        )
    enabled = arguments.get("enabled")
    if not isinstance(enabled, bool):
        raise AgentToolError("enabled 必须是布尔值")
    return {
        "subscription_id": _strict_subscription_id(arguments.get("subscription_id")),
        "enabled": enabled,
    }


def _subscription_identity(
    provider: Any, external_id: Any, media_type: Any
) -> tuple[str, str, str]:
    normalized_provider = unicodedata.normalize(
        "NFKC", str(provider or "")
    ).strip().casefold()
    normalized_external_id = unicodedata.normalize(
        "NFKC", str(external_id or "")
    ).strip()
    normalized_media_type = unicodedata.normalize(
        "NFKC", str(media_type or "")
    ).strip().casefold()
    if normalized_provider not in _ALLOWED_PROVIDERS:
        raise AgentToolError("provider 仅支持 tmdb、douban 或 bangumi")
    if not _PUBLIC_ID_RE.fullmatch(normalized_external_id):
        raise AgentToolError("external_id 格式无效")
    if normalized_media_type not in _ALLOWED_MEDIA_TYPES:
        raise AgentToolError("media_type 仅支持 movie 或 tv")
    return normalized_provider, normalized_external_id, normalized_media_type


def media_subscription_create_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if not set(arguments).issubset({
        "provider", "external_id", "media_type", "season", "check_interval_minutes",
    }):
        raise AgentToolError(
            "media.create_subscription 只接受 provider、external_id、media_type、"
            "可选 season 和 check_interval_minutes 参数"
        )
    if not {"provider", "external_id", "media_type"}.issubset(arguments):
        raise AgentToolError("创建媒体订阅需要精确来源、媒体 ID 和媒体类型")
    provider, external_id, media_type = _subscription_identity(
        arguments.get("provider"),
        arguments.get("external_id"),
        arguments.get("media_type"),
    )
    season = arguments.get("season")
    if season is not None:
        if isinstance(season, bool) or not isinstance(season, int) or not 1 <= season <= 100:
            raise AgentToolError("season 必须是 1 到 100 的整数")
        if media_type != "tv":
            raise AgentToolError("电影订阅不支持季度筛选")
    check_interval_minutes = arguments.get("check_interval_minutes")
    if check_interval_minutes is not None and (
        isinstance(check_interval_minutes, bool)
        or not isinstance(check_interval_minutes, int)
        or check_interval_minutes not in _ALLOWED_CHECK_INTERVALS
    ):
        raise AgentToolError("check_interval_minutes 仅支持 4320（每 3 天）或 10080（每 7 天）")
    normalized = {
        "provider": provider,
        "external_id": external_id,
        "media_type": media_type,
    }
    if season is not None:
        normalized["season"] = int(season)
    if check_interval_minutes is not None:
        normalized["check_interval_minutes"] = int(check_interval_minutes)
    return normalized


def media_subscription_delete_arguments(arguments: dict[str, Any]) -> dict[str, int]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if set(arguments) != {"subscription_id"}:
        raise AgentToolError("media.delete_subscription 只接受 subscription_id 参数")
    return {"subscription_id": _strict_subscription_id(arguments.get("subscription_id"))}


def _media_type_label(value: Any) -> str:
    return "电影" if str(value or "").strip().lower() == "movie" else "剧集"


def _status_label(value: Any, *, enabled: bool) -> str:
    if not enabled:
        return "已暂停"
    return {
        "new": "等待检查",
        "checking": "检查中",
        "satisfied": "当前完整",
        "missing": "发现缺失",
        "inconclusive": "暂无法确认",
        "error": "检查异常",
        "paused": "已暂停",
    }.get(str(value or "").strip().lower(), "状态未知")


def _safe_row(row: Any) -> dict[str, Any]:
    enabled = bool(row["enabled"])
    return {
        "subscription_number": int(row["id"]),
        "title": _safe_title(row["title"], fallback="未命名媒体"),
        "media_type": _media_type_label(row["media_type"]),
        "enabled": enabled,
        "status": _status_label(row["status"], enabled=enabled),
        "missing_count": max(0, int(row["missing_count"] or 0)),
    }


def list_media_subscription_summaries(_arguments: dict[str, Any]) -> ToolResult:
    try:
        total = db.count_media_subscriptions()
        rows = db.list_media_subscriptions(limit=_MAX_SUMMARIES)
    except Exception as exc:
        logger.warning("Agent 媒体追更摘要读取失败 type=%s", type(exc).__name__)
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="暂时无法读取媒体追更订阅",
            data={"total": 0, "returned": 0, "truncated": False, "items": []},
            evidence=[Evidence(
                "media_subscription_database",
                "尝试读取本地媒体追更安全摘要；未读取站点、下载目标、路径或凭据。",
                _now(),
            )],
            suggestions=["请检查本地数据库状态后重试。"],
            error="媒体追更订阅当前不可用。",
        )
    items = [_safe_row(row) for row in rows[:_MAX_SUMMARIES]]
    returned = len(items)
    if total:
        status = "completed"
        summary = f"已读取 {returned} 个媒体追更订阅"
        suggestions = ["如需暂停或恢复，请明确提供一个订阅编号。"]
    else:
        status = "not_configured"
        summary = "尚未创建媒体追更订阅"
        suggestions = ["可直接说：订阅《片名》，再从搜索结果中选择准确条目。"]
    return ToolResult(
        ok=True,
        status=status,
        summary=summary,
        data={
            "total": max(0, int(total or 0)),
            "returned": returned,
            "truncated": int(total or 0) > returned,
            "items": items,
        },
        evidence=[Evidence(
            "media_subscription_database",
            "只读取本地媒体追更编号、标题、类型、启用状态和缺失数量。",
            _now(),
        )],
        suggestions=suggestions,
    )


def _preview_failure(row: Any, *, status: str, summary: str) -> dict[str, Any]:
    return {
        "subscription_number": int(row["id"]),
        "title": _safe_title(row["title"], fallback="未命名媒体"),
        "media_type": str(row["media_type"] or ""),
        "enabled": bool(row["enabled"]),
        "status": status,
        "summary": summary,
        "expected_count": 0,
        "local_count": 0,
        "missing_count": 0,
        "missing": [],
        "inventory_complete": False,
        "sources": [],
        "resource_search": {
            "status": "not_run",
            "attempted_count": 0,
            "candidate_count": 0,
            "truncated": False,
            "items": [],
        },
        "delivery": {
            "state": "unavailable",
            "summary": "本次未形成下载提交建议",
        },
        "checked_at": _now(),
    }


async def _preview_media_subscription_rows(rows: list[Any]) -> list[dict[str, Any]]:
    service = get_media_subscription_service()
    semaphore = asyncio.Semaphore(3)

    async def _one(row: Any) -> dict[str, Any]:
        async with semaphore:
            try:
                return await asyncio.wait_for(
                    service.preview_subscription_updates(
                        int(row["id"]),
                        max_search_episodes=1,
                        limit_per_media=3,
                    ),
                    timeout=_UPDATE_ITEM_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                return _preview_failure(
                    row,
                    status="timeout",
                    summary="实时核对达到单条订阅耗时上限",
                )
            except MediaSubscriptionError as exc:
                return _preview_failure(
                    row,
                    status=str(exc.code or "unavailable")[:40],
                    summary=str(exc)[:160],
                )
            except Exception as exc:
                logger.warning(
                    "Agent 媒体订阅实时核对失败 subscription=%s type=%s",
                    row["id"], type(exc).__name__,
                )
                return _preview_failure(
                    row,
                    status="unavailable",
                    summary="实时核对暂时不可用",
                )

    return list(await asyncio.gather(*(_one(row) for row in rows)))


def inspect_media_subscription_updates(_arguments: dict[str, Any]) -> ToolResult:
    """实时串联订阅、媒体库和资源站；只读，不提交下载或改变调度。"""
    try:
        total = db.count_media_subscriptions()
        rows = db.list_media_subscriptions(limit=_MAX_UPDATE_SUBSCRIPTIONS)
    except Exception as exc:
        logger.warning("Agent 媒体订阅实时列表读取失败 type=%s", type(exc).__name__)
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="暂时无法读取媒体追更订阅",
            data={"total": 0, "returned": 0, "truncated": False, "subscriptions": [], "items": []},
            error="媒体追更订阅当前不可用。",
        )
    if not total:
        return ToolResult(
            ok=True,
            status="not_configured",
            summary="尚未创建媒体追更订阅",
            data={"total": 0, "returned": 0, "truncated": False, "subscriptions": [], "items": []},
            suggestions=["可直接说：订阅《片名》，再从搜索结果中选择准确条目。"],
        )

    try:
        ensure_sync_bridge_available()
        subscriptions = run_indexer_awaitable_sync(
            _preview_media_subscription_rows(rows),
            timeout_seconds=_UPDATE_PREVIEW_TIMEOUT_SECONDS,
        )
    except (AsyncBridgeUnavailable, TimeoutError, RuntimeError) as exc:
        logger.warning("Agent 媒体订阅实时组合核对不可用 type=%s", type(exc).__name__)
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="媒体订阅实时核对当前不可用",
            data={
                "total": max(0, int(total or 0)),
                "returned": 0,
                "truncated": int(total or 0) > len(rows),
                "subscriptions": [],
                "items": [],
            },
            suggestions=["请稍后重试；查询失败不会触发下载或改变后台调度。"],
            error="实时核对暂时不可用。",
        )

    candidate_items: list[dict[str, Any]] = []
    for subscription in subscriptions:
        search = subscription.get("resource_search")
        if not isinstance(search, dict):
            continue
        for searched in search.get("items", []):
            if not isinstance(searched, dict):
                continue
            label = str(searched.get("label") or "")[:24]
            for candidate in searched.get("candidates", []):
                if not isinstance(candidate, dict):
                    continue
                candidate_items.append({
                    **candidate,
                    "media_title": str(subscription.get("title") or "")[:120],
                    "episode_label": label,
                    "subscription_number": int(subscription.get("subscription_number") or 0),
                })
                if len(candidate_items) >= _MAX_UPDATE_CANDIDATES:
                    break
            if len(candidate_items) >= _MAX_UPDATE_CANDIDATES:
                break
        if len(candidate_items) >= _MAX_UPDATE_CANDIDATES:
            break

    updates = sum(1 for item in subscriptions if item.get("status") == "missing")
    current = sum(1 for item in subscriptions if item.get("status") == "satisfied")
    paused = sum(1 for item in subscriptions if item.get("status") == "paused")
    uncertain = len(subscriptions) - updates - current - paused
    partial_searches = sum(
        1
        for item in subscriptions
        if isinstance(item.get("resource_search"), dict)
        and item["resource_search"].get("status") == "partial"
    )
    if updates and (uncertain or partial_searches):
        status = "partial"
        ok = True
        summary = (
            f"已实时核对 {len(subscriptions)} 个媒体追更订阅："
            f"{updates} 个有已播缺失，另有 {uncertain or partial_searches} 个检查未完整完成"
        )
    elif updates:
        status = "updates_available"
        ok = True
        summary = (
            f"已实时核对 {len(subscriptions)} 个媒体追更订阅："
            f"{updates} 个有已播缺失，{current} 个当前完整"
        )
    elif uncertain and not current and not paused:
        status = "unavailable"
        ok = False
        summary = "本次未能可靠核对任何媒体追更订阅"
    elif uncertain:
        status = "partial"
        ok = True
        summary = f"已核对 {len(subscriptions)} 个媒体追更订阅，{uncertain} 个暂无法确认"
    else:
        status = "up_to_date"
        ok = True
        summary = f"已实时核对 {len(subscriptions)} 个媒体追更订阅，当前未发现新的已播缺失"

    suggestions: list[str] = []
    if candidate_items:
        suggestions.append("可回复“第 1 个到光鸭”或“第 1 个到 qB”，进入下载提交确认。")
    if any(
        isinstance(item.get("resource_search"), dict)
        and item["resource_search"].get("truncated")
        for item in subscriptions
    ):
        suggestions.append("有订阅包含多个缺失项；本次只搜索每个订阅最靠前的一项。")
    if uncertain:
        suggestions.append("暂无法确认的订阅应先检查 TMDB、媒体服务器或资源站连通性。")
    elif partial_searches:
        suggestions.append("部分资源站查询未完成；已返回候选仍可查看，未返回不代表没有资源。")
    return ToolResult(
        ok=ok,
        status=status,
        summary=summary,
        data={
            "total": max(0, int(total or 0)),
            "returned": len(subscriptions),
            "truncated": int(total or 0) > len(subscriptions),
            "updates_available_count": updates,
            "up_to_date_count": current,
            "paused_count": paused,
            "inconclusive_count": uncertain,
            "partial_search_count": partial_searches,
            "candidate_count": len(candidate_items),
            "subscriptions": subscriptions,
            # 顶层 items 供会话绑定的“第 N 个到 qB/光鸭”安全续接使用。
            "items": candidate_items,
            "check_definition": "subscription_tmdb_vs_media_servers_with_bounded_indexer_search",
            "read_only": True,
        },
        evidence=[
            Evidence("media_subscription_database", "读取当前媒体追更订阅及其安全策略摘要。", _now()),
            Evidence("media_servers+tmdb", "实时比较 TMDB 已播清单与 Jellyfin/Emby 本地库存。", _now()),
            Evidence("indexer_service", "仅对确认缺失项执行有界多站搜索；未提交下载。", _now()),
        ],
        suggestions=suggestions,
        error="部分订阅暂无法确认。" if uncertain and not ok else "",
    )


def get_media_subscription_summary(arguments: dict[str, int]) -> ToolResult:
    try:
        row = db.get_media_subscription(arguments["subscription_id"])
    except Exception as exc:
        logger.warning("Agent 媒体追更详情读取失败 type=%s", type(exc).__name__)
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="暂时无法读取媒体追更订阅",
            error="媒体追更订阅当前不可用。",
        )
    if row is None:
        raise AgentToolError("未找到指定的媒体追更订阅", code="precondition_failed")
    item = _safe_row(row)
    return ToolResult(
        ok=True,
        status="completed",
        summary=f"媒体追更订阅 {item['subscription_number']}：{item['status']}",
        data=item,
        evidence=[Evidence(
            "media_subscription_database",
            "只读取指定订阅的安全摘要；未读取站点、下载目标、路径或凭据。",
            _now(),
        )],
        suggestions=["可明确说“暂停媒体订阅编号”或“恢复媒体订阅编号”。"],
    )


def _count(conn: Any, table: str, subscription_id: int, status: str) -> int:
    row = conn.execute(
        f"SELECT COUNT(*) AS total FROM {table} WHERE subscription_id=? AND status=?",
        (subscription_id, status),
    ).fetchone()
    return max(0, int((row["total"] if row else 0) or 0))


def _snapshot(conn: Any, subscription_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT id,enabled,status,revision,updated_at FROM media_subscriptions "
        "WHERE id=? AND deleted_at IS NULL",
        (subscription_id,),
    ).fetchone()
    if row is None:
        return {"exists": False, "subscription_id": subscription_id}
    return {
        "exists": True,
        "subscription_id": subscription_id,
        "enabled": bool(row["enabled"]),
        "status": str(row["status"] or ""),
        "revision": int(row["revision"] or 0),
        "updated_at": str(row["updated_at"] or ""),
        "available_candidates": _count(
            conn, "media_subscription_candidates", subscription_id, "available"
        ),
        "claimed_admissions": _count(
            conn, "media_download_admissions", subscription_id, "claimed"
        ),
        "running_checks": _count(
            conn, "media_subscription_runs", subscription_id, "running"
        ),
    }


def _capture(subscription_id: int) -> dict[str, Any]:
    with db.get_conn() as conn:
        return _snapshot(conn, subscription_id)


def _fingerprint(state: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _resolved_tmdb_id(provider: str, external_id: str, media_type: str) -> str:
    if provider == "tmdb":
        if not external_id.isascii() or not external_id.isdigit() or int(external_id) <= 0:
            raise AgentToolError("TMDB 媒体 ID 无效", code="precondition_failed")
        return external_id
    mapping = db.get_media_external_id(provider, external_id, media_type)
    tmdb_id = str(mapping["tmdb_id"] or "").strip() if mapping is not None else ""
    if (
        mapping is None
        or not bool(mapping["confirmed"])
        or not tmdb_id.isascii()
        or not tmdb_id.isdigit()
        or int(tmdb_id) <= 0
    ):
        raise AgentToolError(
            "该来源尚未确认 TMDB 映射，请先在探索页确认媒体身份",
            code="precondition_failed",
        )
    return tmdb_id


def _create_snapshot(tmdb_id: str, media_type: str) -> dict[str, Any]:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT id,enabled,status,revision,deleted_at,updated_at "
            "FROM media_subscriptions WHERE tmdb_id=? AND media_type=?",
            (tmdb_id, media_type),
        ).fetchone()
    if row is None:
        return {"exists": False, "tmdb_id": tmdb_id, "media_type": media_type}
    return {
        "exists": True,
        "subscription_id": int(row["id"]),
        "tmdb_id": tmdb_id,
        "media_type": media_type,
        "enabled": bool(row["enabled"]),
        "status": str(row["status"] or ""),
        "revision": int(row["revision"] or 0),
        "deleted_at": str(row["deleted_at"] or ""),
        "updated_at": str(row["updated_at"] or ""),
    }


def _encode_create_context(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode_create_context(value: str) -> dict[str, Any]:
    try:
        payload = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AgentToolError("确认上下文无效", code="confirmation_invalid") from exc
    if not isinstance(payload, dict):
        raise AgentToolError("确认上下文无效", code="confirmation_invalid")
    return payload


def prepare_create_media_subscription(
    arguments: dict[str, Any],
) -> tuple[ToolResult, str]:
    provider = str(arguments["provider"])
    external_id = str(arguments["external_id"])
    media_type = str(arguments["media_type"])
    season = arguments.get("season")
    check_interval_minutes = int(arguments.get("check_interval_minutes") or 4320)
    tmdb_id = _resolved_tmdb_id(provider, external_id, media_type)
    snapshot = _create_snapshot(tmdb_id, media_type)
    if snapshot.get("exists") and not snapshot.get("deleted_at"):
        raise AgentToolError("该媒体已经在追更订阅中", code="precondition_failed")

    try:
        card = get_discovery_service().get_detail(provider, media_type, external_id)
    except ProviderError as exc:
        raise AgentToolError(
            exc.safe_message or "暂时无法核对该影视条目",
            code="precondition_failed",
        ) from exc
    except Exception as exc:
        logger.warning("Agent 媒体订阅创建预检失败 type=%s", type(exc).__name__)
        raise AgentToolError(
            "暂时无法核对该影视条目", code="confirmation_unavailable"
        ) from exc
    if not isinstance(card, MediaCard) or (
        card.provider, card.external_id, card.media_type
    ) != (provider, external_id, media_type):
        raise AgentToolError("影视数据源返回的条目标识不一致", code="precondition_failed")
    title = _safe_title(card.title, fallback="该媒体")
    year = str(card.year or "").strip()[:12]
    season_label = f"第 {int(season)} 季" if season is not None else "全部缺失内容"
    context = {
        "operation": "create",
        "provider": provider,
        "external_id": external_id,
        "media_type": media_type,
        "tmdb_id": tmdb_id,
        "season": int(season) if season is not None else None,
        "check_interval_minutes": check_interval_minutes,
        "snapshot": snapshot,
    }
    return ToolResult(
        ok=True,
        status="confirmation_required",
        summary=f"确认后将为《{title}》创建媒体追更订阅",
        data={
            "operation": "create",
            "title": title,
            "year": year,
            "media_type": _media_type_label(media_type),
            "monitor_scope": season_label,
            "season": int(season) if season is not None else None,
            "check_interval_minutes": check_interval_minutes,
            "check_interval": "每 7 天" if check_interval_minutes == 10080 else "每 3 天",
            "affected": 1,
            "effects": [
                "确认后只会创建或恢复本地追更订阅，不会立即下载资源。",
                f"新订阅将按{'每 7 天' if check_interval_minutes == 10080 else '每 3 天'}"
                "检查缺失内容，候选下载仍需按策略确认。",
            ],
        },
        evidence=[Evidence(
            "discovery_provider",
            "预检时核对精确影视条目及已确认 TMDB 身份；未搜索资源或提交下载。",
            _now(),
        )],
        suggestions=["确认票据只可使用一次；确认前请核对片名、类型和季度范围。"],
    ), _encode_create_context(context)


def create_media_subscription_confirmed(
    arguments: dict[str, Any], expected_context: str
) -> ToolResult:
    context = _decode_create_context(expected_context)
    identity = (
        str(arguments["provider"]),
        str(arguments["external_id"]),
        str(arguments["media_type"]),
    )
    if (
        context.get("operation") != "create"
        or identity != (
            str(context.get("provider") or ""),
            str(context.get("external_id") or ""),
            str(context.get("media_type") or ""),
        )
        or arguments.get("season") != context.get("season")
        or int(arguments.get("check_interval_minutes") or 4320)
        != int(context.get("check_interval_minutes") or 4320)
    ):
        raise AgentToolError("确认上下文与目标不一致", code="confirmation_invalid")
    tmdb_id = _resolved_tmdb_id(*identity)
    if tmdb_id != str(context.get("tmdb_id") or ""):
        return ToolResult(
            ok=False,
            status="conflict",
            summary="媒体身份映射已变化，请重新预检",
            error="确认快照已失效。",
        )
    snapshot = _create_snapshot(tmdb_id, identity[2])
    if snapshot != context.get("snapshot"):
        return ToolResult(
            ok=False,
            status="conflict",
            summary="媒体追更订阅状态已变化，请重新预检",
            error="确认快照已失效。",
        )
    season = arguments.get("season")
    check_interval_minutes = int(arguments.get("check_interval_minutes") or 4320)
    payload: dict[str, Any] = {
        "provider": identity[0],
        "external_id": identity[1],
        "tmdb_id": tmdb_id,
        "media_type": identity[2],
        "monitor_mode": "selected" if season is not None else "missing",
        "seasons": [int(season)] if season is not None else [],
        "include_specials": False,
        "action": "confirm",
        "download_target": "guangya",
        "check_interval_minutes": check_interval_minutes,
        "enabled": True,
    }
    try:
        ensure_sync_bridge_available()
        created_result = run_indexer_awaitable_sync(
            get_media_subscription_service().create_subscription(
                payload,
                identity_confirmed=identity[0] != "tmdb",
            ),
            timeout_seconds=_CREATE_TIMEOUT_SECONDS,
        )
    except MediaSubscriptionError as exc:
        return ToolResult(
            ok=False,
            status=str(exc.code or "unavailable")[:40],
            summary="媒体追更订阅创建失败",
            error=str(exc)[:240],
        )
    except (AsyncBridgeUnavailable, TimeoutError, RuntimeError) as exc:
        logger.warning("Agent 媒体订阅创建执行失败 type=%s", type(exc).__name__)
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="媒体追更订阅暂时无法创建",
            error="订阅服务暂时不可用。",
        )
    subscription = (
        created_result.get("subscription")
        if isinstance(created_result, dict) else None
    )
    subscription = subscription if isinstance(subscription, dict) else {}
    subscription_id = int(subscription.get("id") or 0)
    created = bool(created_result.get("created")) if isinstance(created_result, dict) else False
    runtime_refreshed = _reload_scheduler()
    return ToolResult(
        ok=True,
        status="completed",
        summary="媒体追更订阅已创建" if created else "媒体追更订阅已恢复",
        data={
            "operation": "create" if created else "restore",
            "subscription_number": subscription_id,
            "title": _safe_title(subscription.get("title"), fallback="该媒体"),
            "media_type": _media_type_label(identity[2]),
            "season": int(season) if season is not None else None,
            "check_interval_minutes": check_interval_minutes,
            "check_interval": "每 7 天" if check_interval_minutes == 10080 else "每 3 天",
            "affected": 1,
            "created": created,
            "runtime_refreshed": runtime_refreshed,
        },
        evidence=[Evidence(
            "media_subscription_database",
            "已使用一次性确认票据创建本地追更订阅；未立即搜索或下载资源。",
            _now(),
        )],
        suggestions=(
            ["订阅已进入后台调度，可继续查看媒体订阅更新。"]
            if runtime_refreshed
            else ["订阅已保存；请重启 MediaFlux 使当前进程重新加载调度。"]
        ),
    )


def prepare_delete_media_subscription(
    arguments: dict[str, Any],
) -> tuple[ToolResult, str]:
    subscription_id = int(arguments["subscription_id"])
    state = _capture(subscription_id)
    if not state.get("exists"):
        raise AgentToolError("未找到指定的媒体追更订阅", code="precondition_failed")
    row = db.get_media_subscription(subscription_id)
    title = _safe_title(row["title"] if row is not None else "", fallback="该媒体")
    return ToolResult(
        ok=True,
        status="confirmation_required",
        summary=f"确认后将删除《{title}》的媒体追更订阅",
        data={
            "operation": "delete",
            "subscription_number": subscription_id,
            "title": title,
            "affected": 1,
            "available_candidates": int(state.get("available_candidates") or 0),
            "claimed_admissions": int(state.get("claimed_admissions") or 0),
            "running_checks": int(state.get("running_checks") or 0),
            "effects": [
                "订阅会被软删除并停止后续检查。",
                "未提交候选、待处理准入和运行中检查会失效；已提交下载和媒体文件不会删除。",
            ],
        },
        evidence=[Evidence(
            "media_subscription_database",
            "只核对目标订阅及未完成工作数量；不会删除下载任务或媒体文件。",
            _now(),
        )],
        suggestions=["删除属于高风险操作；确认票据只可使用一次。"],
    ), _fingerprint(state)


def delete_media_subscription_confirmed(
    arguments: dict[str, Any], expected_context: str
) -> ToolResult:
    subscription_id = int(arguments["subscription_id"])
    state = _capture(subscription_id)
    if not state.get("exists") or not secrets.compare_digest(
        _fingerprint(state), str(expected_context or "")
    ):
        return ToolResult(
            ok=False,
            status="conflict",
            summary="媒体追更订阅状态已变化，请重新预检",
            error="确认快照已失效。",
        )
    removed = get_media_subscription_service().delete_subscription(subscription_id)
    if not removed:
        return ToolResult(
            ok=False,
            status="conflict",
            summary="媒体追更订阅状态已变化，请重新预检",
            error="目标订阅已不存在。",
        )
    runtime_refreshed = _reload_scheduler()
    return ToolResult(
        ok=True,
        status="completed",
        summary="媒体追更订阅已删除",
        data={
            "operation": "delete",
            "subscription_number": subscription_id,
            "affected": 1,
            "expired_candidates": int(state.get("available_candidates") or 0),
            "cancelled_admissions": int(state.get("claimed_admissions") or 0),
            "cancelled_runs": int(state.get("running_checks") or 0),
            "runtime_refreshed": runtime_refreshed,
        },
        evidence=[Evidence(
            "media_subscription_database",
            "已使用一次性确认票据软删除订阅；已提交下载与媒体文件未被删除。",
            _now(),
        )],
        suggestions=(
            ["订阅已删除，后续不会再安排检查。"]
            if runtime_refreshed
            else ["订阅已删除；请重启 MediaFlux 使当前进程重新加载调度。"]
        ),
    )


def prepare_set_media_subscription_enabled(
    arguments: dict[str, Any],
) -> tuple[ToolResult, str]:
    subscription_id = int(arguments["subscription_id"])
    state = _capture(subscription_id)
    if not state.get("exists"):
        raise AgentToolError("未找到指定的媒体追更订阅", code="precondition_failed")
    requested = bool(arguments["enabled"])
    if bool(state["enabled"]) == requested:
        label = "启用" if requested else "暂停"
        raise AgentToolError(f"该媒体追更订阅已经{label}", code="precondition_failed")
    row = db.get_media_subscription(subscription_id)
    title = _safe_title(row["title"] if row is not None else "", fallback="该媒体")
    effects = (
        ["恢复后会重新进入检查队列，并在下一次调度时核对媒体库。"]
        if requested
        else [
            "暂停后不会安排新的检查。",
            "尚未提交的候选资源和检查任务会失效；已提交的下载任务与文件不受影响。",
        ]
    )
    preview = ToolResult(
        ok=True,
        status="confirmation_required",
        summary=f"确认后将{'恢复' if requested else '暂停'}《{title}》的媒体追更",
        data={
            "operation": "enable" if requested else "disable",
            "subscription_number": subscription_id,
            "title": title,
            "enabled": requested,
            "affected": 1,
            "effects": effects,
        },
        evidence=[Evidence(
            "media_subscription_database",
            "仅核对本地订阅状态和待失效记录数量；未访问资源网站或下载器。",
            _now(),
        )],
        suggestions=["确认票据只可使用一次；订阅状态变化后需要重新预检。"],
    )
    return preview, _fingerprint(state)


def _reload_scheduler() -> bool:
    try:
        from app.modules.media_subscription_scheduler import get_media_subscription_scheduler

        get_media_subscription_scheduler().reload()
        return True
    except Exception as exc:
        logger.warning("Agent 媒体追更配置已保存但调度器刷新失败 type=%s", type(exc).__name__)
        return False


def set_media_subscription_enabled_confirmed(
    arguments: dict[str, Any], expected_context: str
) -> ToolResult:
    subscription_id = int(arguments["subscription_id"])
    requested = bool(arguments["enabled"])
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        state = _snapshot(conn, subscription_id)
        if not state.get("exists") or not secrets.compare_digest(
            _fingerprint(state), str(expected_context or "")
        ):
            return ToolResult(
                ok=False,
                status="conflict",
                summary="媒体追更订阅状态已变化，请重新预检",
                error="确认快照已失效。",
            )
        stamp = db.now()
        cur = conn.execute(
            "UPDATE media_subscriptions SET enabled=?,status=?,next_check_at=?,last_error='',"
            "revision=revision+1,updated_at=? WHERE id=? AND deleted_at IS NULL",
            (1 if requested else 0, "new" if requested else "paused", stamp, stamp, subscription_id),
        )
        if cur.rowcount != 1:
            return ToolResult(
                ok=False,
                status="conflict",
                summary="媒体追更订阅状态已变化，请重新预检",
                error="目标订阅已不存在。",
            )
        expired = 0
        cancelled_admissions = 0
        cancelled_runs = 0
        if not requested:
            expired = conn.execute(
                "UPDATE media_subscription_candidates SET status='expired',updated_at=? "
                "WHERE subscription_id=? AND status='available'",
                (stamp, subscription_id),
            ).rowcount
            cancelled_admissions = conn.execute(
                "UPDATE media_download_admissions SET status='cancelled',error=?,completed_at=?,updated_at=? "
                "WHERE subscription_id=? AND status='claimed'",
                ("订阅已暂停", stamp, stamp, subscription_id),
            ).rowcount
            cancelled_runs = conn.execute(
                "UPDATE media_subscription_runs SET status='cancelled',summary=?,error=?,finished_at=? "
                "WHERE subscription_id=? AND status='running'",
                ("订阅已暂停，当前检查已取消", "订阅已暂停", stamp, subscription_id),
            ).rowcount

    runtime_refreshed = _reload_scheduler()
    return ToolResult(
        ok=True,
        status="completed",
        summary="媒体追更已恢复" if requested else "媒体追更已暂停",
        data={
            "operation": "enable" if requested else "disable",
            "subscription_number": subscription_id,
            "enabled": requested,
            "affected": 1,
            "expired_candidates": max(0, int(expired or 0)),
            "cancelled_admissions": max(0, int(cancelled_admissions or 0)),
            "cancelled_runs": max(0, int(cancelled_runs or 0)),
            "runtime_refreshed": runtime_refreshed,
        },
        evidence=[Evidence(
            "media_subscription_database",
            "已使用一次性确认票据原子更新订阅；未操作已提交下载任务或媒体文件。",
            _now(),
        )],
        suggestions=(
            ["追更已恢复，下一次调度会重新检查媒体库。"]
            if requested and runtime_refreshed
            else ["追更已暂停；已提交下载任务和文件不会受影响。"]
            if not requested
            else ["配置已保存；请重启 MediaFlux 使调度器在当前进程重新读取配置。"]
        ),
    )


_ALLOWED_MONITOR_MODES = frozenset({"missing", "future", "selected"})
_ALLOWED_SUBSCRIPTION_ACTIONS = frozenset({"notify", "confirm", "auto"})
_ALLOWED_DOWNLOAD_TARGETS = frozenset({"qb", "guangya", "both"})


def media_subscription_policy_arguments(arguments: dict[str, Any]) -> dict[str, int]:
    return media_subscription_summary_arguments(arguments)


def media_subscription_policy_update_arguments(
    arguments: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    allowed = {
        "subscription_id", "monitor_mode", "seasons", "include_specials",
        "action", "download_target", "check_interval_minutes",
    }
    if not set(arguments).issubset(allowed) or "subscription_id" not in arguments:
        raise AgentToolError("媒体追更策略参数无效")
    normalized: dict[str, Any] = {
        "subscription_id": _strict_subscription_id(arguments.get("subscription_id"))
    }
    if len(arguments) == 1:
        raise AgentToolError("至少需要提供一个要修改的追更策略字段")
    if "monitor_mode" in arguments:
        mode = str(arguments.get("monitor_mode") or "").strip().lower()
        if mode not in _ALLOWED_MONITOR_MODES:
            raise AgentToolError("monitor_mode 仅支持 missing、future 或 selected")
        normalized["monitor_mode"] = mode
    if "seasons" in arguments:
        raw_seasons = arguments.get("seasons")
        if not isinstance(raw_seasons, list) or len(raw_seasons) > 20:
            raise AgentToolError("seasons 必须是最多 20 项的季度数组")
        seasons: list[int] = []
        for value in raw_seasons:
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
                raise AgentToolError("季度编号必须是 1 到 100 的整数")
            if value not in seasons:
                seasons.append(value)
        normalized["seasons"] = sorted(seasons)
    if "include_specials" in arguments:
        value = arguments.get("include_specials")
        if not isinstance(value, bool):
            raise AgentToolError("include_specials 必须是布尔值")
        normalized["include_specials"] = value
    if "action" in arguments:
        action = str(arguments.get("action") or "").strip().lower()
        if action not in _ALLOWED_SUBSCRIPTION_ACTIONS:
            raise AgentToolError("action 仅支持 notify、confirm 或 auto")
        normalized["action"] = action
    if "download_target" in arguments:
        target = str(arguments.get("download_target") or "").strip().lower()
        if target not in _ALLOWED_DOWNLOAD_TARGETS:
            raise AgentToolError("download_target 仅支持 qb、guangya 或 both")
        normalized["download_target"] = target
    if "check_interval_minutes" in arguments:
        interval = arguments.get("check_interval_minutes")
        if (
            isinstance(interval, bool)
            or not isinstance(interval, int)
            or not 5 <= interval <= 10080
        ):
            raise AgentToolError("check_interval_minutes 必须是 5 到 10080 的整数")
        normalized["check_interval_minutes"] = interval
    mode = normalized.get("monitor_mode")
    seasons = normalized.get("seasons")
    if mode == "selected" and seasons == []:
        raise AgentToolError("selected 模式至少需要一个季度")
    return normalized


def _json_int_list(value: Any) -> list[int]:
    try:
        raw = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    result: list[int] = []
    for item in raw[:20]:
        if isinstance(item, int) and not isinstance(item, bool) and 1 <= item <= 100:
            if item not in result:
                result.append(item)
    return sorted(result)


def _policy_projection(row: Any) -> dict[str, Any]:
    return {
        "subscription_number": int(row["id"]),
        "title": _safe_title(row["title"], fallback="未命名媒体"),
        "media_type": str(row["media_type"] or ""),
        "enabled": bool(row["enabled"]),
        "monitor_mode": str(row["monitor_mode"] or "missing"),
        "seasons": _json_int_list(row["seasons_json"]),
        "include_specials": bool(row["include_specials"]),
        "action": str(row["action"] or "confirm"),
        "download_target": str(row["download_target"] or "guangya"),
        "check_interval_minutes": max(5, min(int(row["check_interval_minutes"] or 4320), 10080)),
        "site_count": len(_json_string_list(row["sites_json"])),
    }


def _json_string_list(value: Any) -> list[str]:
    try:
        raw = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    return [str(item)[:32] for item in raw[:16] if isinstance(item, str) and item]


def get_media_subscription_policy(arguments: dict[str, int]) -> ToolResult:
    row = db.get_media_subscription(arguments["subscription_id"])
    if row is None:
        raise AgentToolError("未找到指定的媒体追更订阅", code="precondition_failed")
    policy = _policy_projection(row)
    return ToolResult(
        True,
        "completed",
        f"已读取《{policy['title']}》的追更策略",
        data=policy,
        evidence=[Evidence(
            "media_subscription_database",
            "只读取追更范围、动作模式、下载目标和检查周期；不返回站点明细或凭据。",
            _now(),
        )],
        suggestions=[f"如需修改，请明确说媒体订阅 {policy['subscription_number']} 要改哪些策略。"],
    )


def _policy_snapshot(conn: Any, subscription_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT id,title,media_type,enabled,monitor_mode,seasons_json,include_specials,"
        "action,download_target,sites_json,check_interval_minutes,status,revision,updated_at "
        "FROM media_subscriptions WHERE id=? AND deleted_at IS NULL",
        (subscription_id,),
    ).fetchone()
    if row is None:
        return {"exists": False, "subscription_id": subscription_id}
    return {
        "exists": True,
        **_policy_projection(row),
        "status": str(row["status"] or ""),
        "revision": int(row["revision"] or 0),
        "updated_at": str(row["updated_at"] or ""),
    }


def _effective_subscription_policy(
    snapshot: dict[str, Any], normalized: dict[str, Any]
) -> dict[str, Any]:
    return {
        "monitor_mode": normalized.get("monitor_mode", snapshot.get("monitor_mode", "missing")),
        "seasons": list(normalized.get("seasons", snapshot.get("seasons", [])) or []),
        "include_specials": bool(
            normalized.get("include_specials", snapshot.get("include_specials", False))
        ),
        "action": normalized.get("action", snapshot.get("action", "confirm")),
        "download_target": normalized.get(
            "download_target", snapshot.get("download_target", "guangya")
        ),
        "check_interval_minutes": normalized.get(
            "check_interval_minutes", snapshot.get("check_interval_minutes", 4320)
        ),
    }


def _validate_effective_subscription_policy(
    snapshot: dict[str, Any], normalized: dict[str, Any]
) -> dict[str, Any]:
    effective = _effective_subscription_policy(snapshot, normalized)
    media_type = str(snapshot.get("media_type") or "").strip().lower()
    if media_type == "tv" and (
        effective["monitor_mode"] == "selected" and not effective["seasons"]
    ):
        raise AgentToolError(
            "selected 模式至少需要一个季度", code="precondition_failed"
        )
    if media_type == "movie" and (
        effective["monitor_mode"] == "selected"
        or bool(effective["seasons"])
        or effective["include_specials"]
    ):
        raise AgentToolError(
            "电影订阅不支持按季度或特别篇追更", code="precondition_failed"
        )
    return effective


def _active_admission_state(conn: Any, subscription_id: int) -> dict[str, Any]:
    """生成策略确认窗口内 active admission 的有界公开计数与内部状态摘要。"""
    rows = conn.execute(
        "SELECT id,subscription_revision,status,updated_at "
        "FROM media_download_admissions WHERE subscription_id=? "
        "AND status IN ('claimed','dispatching') ORDER BY id",
        (subscription_id,),
    ).fetchall()
    digest = hashlib.sha256()
    counts = {"claimed": 0, "dispatching": 0}
    for row in rows:
        status = str(row["status"] or "")
        if status in counts:
            counts[status] += 1
        digest.update(
            (
                f"{int(row['id'])}:{int(row['subscription_revision'] or 0)}:"
                f"{status}:{str(row['updated_at'] or '')}\n"
            ).encode("utf-8")
        )
    return {
        **counts,
        "total": len(rows),
        "state_digest": digest.hexdigest(),
    }


def _policy_confirmation_snapshot(conn: Any, subscription_id: int) -> dict[str, Any]:
    snapshot = _policy_snapshot(conn, subscription_id)
    if snapshot.get("exists"):
        snapshot["_active_admissions"] = _active_admission_state(
            conn, subscription_id
        )
    return snapshot


def _policy_preview_effects(
    *, snapshot: dict[str, Any], effective: dict[str, Any], dispatching: int
) -> list[str]:
    effects = [
        "保存后会使旧候选和未提交准入失效",
        "本操作不会主动检查更新或创建新的下载提交",
    ]
    if effective["action"] == "auto":
        target = {
            "qb": "qBittorrent",
            "guangya": "光鸭",
            "both": "qBittorrent 和光鸭",
        }.get(str(effective["download_target"]), "所选下载目标")
        effects.append(
            f"后续调度确认匹配后会无需再次人工确认，自动提交到{target}"
        )
    else:
        effects.append("后续匹配资源仍会按通知或人工确认模式处理")
    if snapshot.get("action") == "auto" or dispatching:
        effects.append(
            f"预检时有 {dispatching} 个任务已进入提交阶段；这些任务无法由本次策略修改撤回"
        )
    return effects


def prepare_set_media_subscription_policy(
    arguments: dict[str, Any],
) -> tuple[ToolResult, str]:
    normalized = media_subscription_policy_update_arguments(arguments)
    subscription_id = normalized["subscription_id"]
    with db.get_conn() as conn:
        snapshot = _policy_confirmation_snapshot(conn, subscription_id)
        active_admissions = snapshot.get("_active_admissions", {})
        dispatching = int(active_admissions.get("dispatching") or 0)
        claimed = int(active_admissions.get("claimed") or 0)
    if not snapshot.get("exists"):
        raise AgentToolError("未找到指定的媒体追更订阅", code="precondition_failed")
    effective = _validate_effective_subscription_policy(snapshot, normalized)
    requested = {key: value for key, value in normalized.items() if key != "subscription_id"}
    preview = ToolResult(
        True,
        "confirmation_required",
        f"确认后将修改《{snapshot['title']}》的追更策略",
        data={
            "subscription_number": subscription_id,
            "title": snapshot["title"],
            "current": {key: snapshot.get(key) for key in requested},
            "requested": requested,
            "effective": effective,
            "in_flight_dispatches_at_preflight": dispatching,
            "pending_admissions_at_preflight": claimed,
            "effects": _policy_preview_effects(
                snapshot=snapshot,
                effective=effective,
                dispatching=dispatching,
            ),
        },
        evidence=[Evidence(
            "media_subscription_database",
            "已读取订阅修订号和当前安全策略，尚未修改数据库。",
            _now(),
        )],
        suggestions=["确认票据只可使用一次；策略变化后需要重新预检。"],
    )
    return preview, _fingerprint(snapshot)


def set_media_subscription_policy_confirmed(
    arguments: dict[str, Any], expected_context: str
) -> ToolResult:
    normalized = media_subscription_policy_update_arguments(arguments)
    subscription_id = normalized.pop("subscription_id")
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        snapshot = _policy_confirmation_snapshot(conn, subscription_id)
        if not snapshot.get("exists") or not secrets.compare_digest(
            _fingerprint(snapshot), str(expected_context or "")
        ):
            return ToolResult(False, "conflict", "追更策略已变化，请重新预检", error="确认快照已失效。")
        try:
            effective = _validate_effective_subscription_policy(snapshot, normalized)
        except AgentToolError as exc:
            return ToolResult(False, "conflict", exc.public_message, error=exc.public_message)
        dispatching = int(
            snapshot.get("_active_admissions", {}).get("dispatching") or 0
        )
        fields: dict[str, Any] = {}
        for key, value in normalized.items():
            fields["seasons_json" if key == "seasons" else key] = (
                json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                if key == "seasons"
                else int(value) if key == "include_specials" else value
            )
        if not fields:
            return ToolResult(False, "conflict", "没有可修改的追更策略", error="请求为空。")
        fields["next_check_at"] = db.now()
        fields["status"] = "new" if snapshot["enabled"] else "paused"
        fields["last_error"] = ""
        assignments = ",".join(f"{key}=?" for key in fields)
        stamp = db.now()
        cur = conn.execute(
            f"UPDATE media_subscriptions SET {assignments},revision=revision+1,updated_at=? "
            "WHERE id=? AND deleted_at IS NULL AND revision=?",
            (*fields.values(), stamp, subscription_id, snapshot["revision"]),
        )
        if cur.rowcount != 1:
            return ToolResult(False, "conflict", "追更策略已变化，请重新预检", error="目标订阅已变化。")
        expired = conn.execute(
            "UPDATE media_subscription_candidates SET status='expired',updated_at=? "
            "WHERE subscription_id=? AND status='available'",
            (stamp, subscription_id),
        ).rowcount
        conn.execute(
            "UPDATE media_download_admissions SET status='cancelled',error=?,completed_at=?,updated_at=? "
            "WHERE subscription_id=? AND status='claimed'",
            ("订阅策略已变更", stamp, stamp, subscription_id),
        )
        conn.execute(
            "UPDATE media_subscription_runs SET status='cancelled',summary=?,error=?,finished_at=? "
            "WHERE subscription_id=? AND status='running'",
            ("订阅策略已变更，旧检查已取消", "订阅策略已变更", stamp, subscription_id),
        )
    refreshed = _reload_scheduler()
    return ToolResult(
        True,
        "completed",
        "媒体追更策略已更新",
        data={
            "subscription_number": subscription_id,
            "updated_fields": sorted(normalized),
            "expired_candidates": max(0, int(expired or 0)),
            "in_flight_dispatches": dispatching,
            "effective_action": effective["action"],
            "runtime_refreshed": refreshed,
        },
        evidence=[Evidence(
            "media_subscription_database",
            "使用一次性确认票据和修订号原子更新策略；未立即检查或下载。",
            _now(),
        )],
        suggestions=[
            "策略已保存，下一次调度会按新策略重新检查。",
            *(
                [f"仍有 {dispatching} 个已进入提交阶段的旧任务无法撤回。"]
                if dispatching
                else []
            ),
        ],
    )
