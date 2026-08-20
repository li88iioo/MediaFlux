"""影视探索收藏的安全摘要与确认写入。"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re
from typing import Any, Callable
import unicodedata

from app import config, database as db
from app.agent.models import Evidence, ToolResult
from app.agent.registry import AgentToolError
from app.discovery.models import MediaCard, ProviderError
from app.discovery.service import get_discovery_service

_ALLOWED_PROVIDERS = {"tmdb", "douban", "bangumi"}
_ALLOWED_MEDIA_TYPES = {"movie", "tv"}
_PUBLIC_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,180}$")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _safe_text(value: Any, limit: int) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip()
    cleaned = "".join(
        " " if unicodedata.category(char).startswith("C") else char
        for char in normalized
    )
    return " ".join(cleaned.split())[:limit]


def _positive_id(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AgentToolError(f"{field} 必须是正整数")
    return int(value)


def _identity(provider: Any, external_id: Any, media_type: Any) -> tuple[str, str, str]:
    normalized_provider = _safe_text(provider, 20).casefold()
    normalized_external_id = unicodedata.normalize(
        "NFKC", str(external_id or "")
    ).strip()[:180]
    normalized_media_type = _safe_text(media_type, 12).casefold()
    if normalized_provider not in _ALLOWED_PROVIDERS:
        raise AgentToolError("provider 仅支持 tmdb、douban 或 bangumi")
    if not _PUBLIC_ID_RE.fullmatch(normalized_external_id):
        raise AgentToolError("external_id 格式无效")
    if normalized_media_type not in _ALLOWED_MEDIA_TYPES:
        raise AgentToolError("media_type 仅支持 movie 或 tv")
    return normalized_provider, normalized_external_id, normalized_media_type


def watchlist_summaries_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if arguments:
        raise AgentToolError("discovery.watchlist_summaries 不接受参数")
    return {}


def watchlist_summary_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if set(arguments) != {"watchlist_number"}:
        raise AgentToolError("必须且只能提供 watchlist_number")
    return {"watchlist_number": _positive_id(arguments["watchlist_number"], field="watchlist_number")}


def add_watchlist_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if set(arguments) != {"provider", "external_id", "media_type"}:
        raise AgentToolError("必须且只能提供 provider、external_id 和 media_type")
    provider, external_id, media_type = _identity(
        arguments["provider"], arguments["external_id"], arguments["media_type"]
    )
    return {
        "provider": provider,
        "external_id": external_id,
        "media_type": media_type,
    }


def remove_watchlist_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    return watchlist_summary_arguments(arguments)


def _feature_disabled() -> ToolResult:
    return ToolResult(
        ok=False,
        status="disabled",
        summary="影视探索功能当前已关闭",
        evidence=[Evidence("discovery_config", "检查影视探索总开关。", _now())],
        suggestions=["请先在设置中启用影视探索。"],
        error="影视探索功能未启用。",
    )


def _row_summary(row: Any) -> dict[str, Any]:
    return {
        "watchlist_number": int(row["id"]),
        "provider": _safe_text(row["provider"], 20),
        "media_type": _safe_text(row["media_type"], 12),
        "title": _safe_text(row["title"], 160) or "未命名条目",
        "year": _safe_text(row["year"], 12),
    }


def list_watchlist_summaries(_arguments: dict[str, Any]) -> ToolResult:
    if not config.get_bool("DISCOVERY_ENABLED", False):
        return _feature_disabled()
    rows = db.list_media_watchlist(limit=20)
    items = [_row_summary(row) for row in rows]
    count = len(items)
    return ToolResult(
        ok=True,
        status="success" if count else "empty",
        summary=f"探索收藏中共有 {count} 项" if count else "探索收藏当前为空",
        data={"count": count, "items": items},
        evidence=[Evidence(
            "discovery_watchlist",
            "仅读取本地探索收藏的编号、来源、类型、标题和年份。",
            _now(),
        )],
        suggestions=(
            ["可按收藏编号查看单项，或移除明确编号的收藏。"] if count else []
        ),
    )


def get_watchlist_summary(arguments: dict[str, Any]) -> ToolResult:
    if not config.get_bool("DISCOVERY_ENABLED", False):
        return _feature_disabled()
    number = int(arguments["watchlist_number"])
    row = db.get_media_watchlist_by_id(number)
    if row is None:
        return ToolResult(
            ok=False,
            status="not_found",
            summary=f"没有找到编号 {number} 的探索收藏",
            error="目标收藏不存在。",
        )
    item = _row_summary(row)
    return ToolResult(
        ok=True,
        status="success",
        summary=f"探索收藏 {number}：{item['title']}",
        data=item,
        evidence=[Evidence(
            "discovery_watchlist",
            "仅读取本地探索收藏的公开摘要。",
            _now(),
        )],
    )


def _snapshot_payload(row: Any) -> dict[str, Any]:
    if row is None:
        return {"exists": False}
    return {
        "exists": True,
        "watchlist_number": int(row["id"]),
        "provider": str(row["provider"] or ""),
        "external_id": str(row["external_id"] or ""),
        "media_type": str(row["media_type"] or ""),
        "title": str(row["title"] or ""),
        "year": str(row["year"] or ""),
        "poster_key": str(row["poster_key"] or ""),
        "created_at": str(row["created_at"] or ""),
    }


def _fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _encode_context(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode_context(value: str) -> dict[str, Any]:
    try:
        payload = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AgentToolError("确认上下文无效", code="confirmation_invalid") from exc
    if not isinstance(payload, dict):
        raise AgentToolError("确认上下文无效", code="confirmation_invalid")
    return payload


def _canonical_card(card: MediaCard, expected: tuple[str, str, str]) -> dict[str, Any]:
    identity = (card.provider, card.external_id, card.media_type)
    if identity != expected:
        raise AgentToolError("影视数据源返回的条目标识不一致")
    title = _safe_text(card.title, 160)
    if not title:
        raise AgentToolError("影视条目缺少可用标题")
    return {
        "provider": card.provider,
        "external_id": card.external_id,
        "media_type": card.media_type,
        "title": title,
        "year": _safe_text(card.year, 12),
        "poster_key": str(card.poster_key or "")[:500],
    }


def prepare_add_watchlist(arguments: dict[str, Any]) -> tuple[ToolResult, str]:
    if not config.get_bool("DISCOVERY_ENABLED", False):
        return _feature_disabled(), ""
    identity = (
        str(arguments["provider"]),
        str(arguments["external_id"]),
        str(arguments["media_type"]),
    )
    if db.get_media_watchlist(*identity) is not None:
        return ToolResult(
            ok=False,
            status="no_changes",
            summary="该影视条目已在探索收藏中",
            error="无需重复添加。",
        ), ""
    try:
        card = get_discovery_service().get_detail(identity[0], identity[2], identity[1])
    except ProviderError as exc:
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="暂时无法核对该影视条目",
            error=exc.safe_message or "影视数据源暂时不可用。",
        ), ""
    except Exception:
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="暂时无法核对该影视条目",
            error="影视数据源暂时不可用。",
        ), ""
    if not isinstance(card, MediaCard):
        return ToolResult(
            ok=False,
            status="not_found",
            summary="没有找到可加入收藏的影视条目",
            error="目标条目不存在。",
        ), ""
    canonical = _canonical_card(card, identity)
    context = {
        "operation": "add",
        "canonical": canonical,
        "expected_absent": True,
    }
    preview = ToolResult(
        ok=True,
        status="confirmation_required",
        summary=f"确认后将《{canonical['title']}》加入探索收藏",
        data={
            "operation": "add",
            "title": canonical["title"],
            "year": canonical["year"],
            "provider": canonical["provider"],
            "media_type": canonical["media_type"],
            "affected": 1,
        },
        evidence=[Evidence(
            "discovery_provider",
            "预检时核对影视条目；确认执行只写入本地收藏，不会下载资源。",
            _now(),
        )],
        suggestions=["确认票据只可使用一次；收藏状态变化后需要重新预检。"],
    )
    return preview, _encode_context(context)


def add_watchlist_confirmed(arguments: dict[str, Any], expected_context: str) -> ToolResult:
    context = _decode_context(expected_context)
    canonical = context.get("canonical")
    if context.get("operation") != "add" or not isinstance(canonical, dict):
        raise AgentToolError("确认上下文无效", code="confirmation_invalid")
    identity = (
        str(arguments["provider"]),
        str(arguments["external_id"]),
        str(arguments["media_type"]),
    )
    if identity != (
        str(canonical.get("provider") or ""),
        str(canonical.get("external_id") or ""),
        str(canonical.get("media_type") or ""),
    ):
        raise AgentToolError("确认上下文与目标不一致", code="confirmation_invalid")
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id FROM media_watchlist WHERE provider=? AND external_id=? AND media_type=?",
            identity,
        ).fetchone()
        if row is not None:
            return ToolResult(
                ok=False,
                status="conflict",
                summary="探索收藏状态已变化，请重新预检",
                error="目标条目已被加入收藏。",
            )
        cur = conn.execute(
            "INSERT INTO media_watchlist(provider,external_id,media_type,title,year,poster_key,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                identity[0], identity[1], identity[2],
                str(canonical.get("title") or "")[:160],
                str(canonical.get("year") or "")[:12],
                str(canonical.get("poster_key") or "")[:500],
                db.now(),
            ),
        )
        watchlist_number = int(cur.lastrowid)
    return ToolResult(
        ok=True,
        status="completed",
        summary=f"已将《{_safe_text(canonical.get('title'), 160)}》加入探索收藏",
        data={
            "operation": "add",
            "watchlist_number": watchlist_number,
            "title": _safe_text(canonical.get("title"), 160),
            "affected": 1,
        },
        evidence=[Evidence(
            "discovery_watchlist",
            "已使用一次性确认票据写入本地探索收藏；未提交下载任务。",
            _now(),
        )],
    )


def prepare_remove_watchlist(arguments: dict[str, Any]) -> tuple[ToolResult, str]:
    if not config.get_bool("DISCOVERY_ENABLED", False):
        return _feature_disabled(), ""
    number = int(arguments["watchlist_number"])
    row = db.get_media_watchlist_by_id(number)
    if row is None:
        return ToolResult(
            ok=False,
            status="not_found",
            summary=f"没有找到编号 {number} 的探索收藏",
            error="目标收藏不存在。",
        ), ""
    snapshot = _snapshot_payload(row)
    title = _safe_text(snapshot.get("title"), 160) or "未命名条目"
    context = {
        "operation": "remove",
        "snapshot": snapshot,
        "fingerprint": _fingerprint(snapshot),
    }
    preview = ToolResult(
        ok=True,
        status="confirmation_required",
        summary=f"确认后将从探索收藏移除《{title}》",
        data={
            "operation": "remove",
            "watchlist_number": number,
            "title": title,
            "affected": 1,
        },
        evidence=[Evidence(
            "discovery_watchlist",
            "仅预检本地探索收藏；不会删除媒体文件、订阅或下载任务。",
            _now(),
        )],
        suggestions=["确认票据只可使用一次；收藏状态变化后需要重新预检。"],
    )
    return preview, _encode_context(context)


def remove_watchlist_confirmed(arguments: dict[str, Any], expected_context: str) -> ToolResult:
    context = _decode_context(expected_context)
    snapshot = context.get("snapshot")
    expected_fingerprint = str(context.get("fingerprint") or "")
    number = int(arguments["watchlist_number"])
    if (
        context.get("operation") != "remove"
        or not isinstance(snapshot, dict)
        or int(snapshot.get("watchlist_number") or 0) != number
        or _fingerprint(snapshot) != expected_fingerprint
    ):
        raise AgentToolError("确认上下文无效", code="confirmation_invalid")
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM media_watchlist WHERE id=?", (number,)).fetchone()
        current = _snapshot_payload(row)
        if row is None or _fingerprint(current) != expected_fingerprint:
            return ToolResult(
                ok=False,
                status="conflict",
                summary="探索收藏状态已变化，请重新预检",
                error="确认快照已失效。",
            )
        deleted = conn.execute("DELETE FROM media_watchlist WHERE id=?", (number,)).rowcount
        if deleted != 1:
            return ToolResult(
                ok=False,
                status="conflict",
                summary="探索收藏状态已变化，请重新预检",
                error="目标收藏已不存在。",
            )
    title = _safe_text(snapshot.get("title"), 160) or "该条目"
    return ToolResult(
        ok=True,
        status="completed",
        summary=f"已从探索收藏移除《{title}》",
        data={
            "operation": "remove",
            "watchlist_number": number,
            "title": title,
            "affected": 1,
        },
        evidence=[Evidence(
            "discovery_watchlist",
            "已使用一次性确认票据删除本地收藏记录；未删除媒体文件或下载任务。",
            _now(),
        )],
    )


def _unconfirmed(_arguments: dict[str, Any]) -> ToolResult:
    raise AgentToolError("该探索收藏操作需要确认", code="confirmation_required")


add_watchlist: Callable[[dict[str, Any]], ToolResult] = _unconfirmed
remove_watchlist: Callable[[dict[str, Any]], ToolResult] = _unconfirmed
