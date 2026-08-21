"""媒体追更订阅的安全摘要与单条启停动作。"""
from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
import json
import secrets
from typing import Any, Callable

from app import database as db
from app.agent.async_bridge import AsyncBridgeUnavailable, ensure_sync_bridge_available
from app.agent.models import Evidence, ToolResult
from app.agent.registry import AgentToolError
from app.agent.workspace_actions import _safe_title
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


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _strict_subscription_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
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
        suggestions = ["可先从媒体探索页为一部电影或剧集创建追更订阅。"]
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
            suggestions=["可先从媒体探索页为电影或剧集创建追更订阅。"],
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
    if updates:
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


def _unconfirmed(_arguments: dict[str, Any]) -> ToolResult:
    raise AgentToolError("该媒体追更订阅操作需要确认", code="confirmation_required")


set_media_subscription_enabled: Callable[[dict[str, Any]], ToolResult] = _unconfirmed
