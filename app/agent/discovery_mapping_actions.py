"""发现详情、跨源 TMDB 候选与确认映射。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
import re
import threading
import time
from typing import Any

from app import config, database as db
from app.agent.confirmation import confirmation_context_fingerprint
from app.agent.models import Evidence, ToolContext, ToolResult
from app.agent.registry import AgentToolError
from app.agent.result_projection import sanitize_public_text
from app.agent.session_context import (
    AgentContextWriteGuard,
    AgentSessionContextRepository,
)
from app.discovery.service import get_discovery_service

_ALLOWED_PROVIDERS = {"tmdb", "douban", "bangumi"}
_ALLOWED_MEDIA_TYPES = {"movie", "tv"}
_PUBLIC_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,180}$")


@dataclass
class _MappingSnapshot:
    owner: str
    provider: str
    external_id: str
    media_type: str
    title: str
    year: str
    candidates: list[dict[str, Any]]
    created_at: float
    generation: int = 0
    revision: int = 0


_lock = threading.RLock()
_recent: dict[str, _MappingSnapshot] = {}
_TTL_SECONDS = 600.0
_CONTEXT_TYPE = "discovery_mapping"
_repository: AgentSessionContextRepository | None = None
logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _safe(value: Any, limit: int) -> str:
    return sanitize_public_text(value, limit=limit)


def _identity(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or set(arguments) != {"provider", "external_id", "media_type"}:
        raise AgentToolError("必须且只能提供 provider、external_id 和 media_type")
    provider = str(arguments["provider"] or "").strip().casefold()
    external_id = str(arguments["external_id"] or "").strip()
    media_type = str(arguments["media_type"] or "").strip().casefold()
    if provider not in _ALLOWED_PROVIDERS:
        raise AgentToolError("provider 仅支持 tmdb、douban 或 bangumi")
    if media_type not in _ALLOWED_MEDIA_TYPES:
        raise AgentToolError("media_type 仅支持 movie 或 tv")
    if not _PUBLIC_ID_RE.fullmatch(external_id):
        raise AgentToolError("external_id 格式无效")
    return {"provider": provider, "external_id": external_id, "media_type": media_type}


def discovery_detail_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    return _identity(arguments)


def discovery_mapping_candidates_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    return _identity(arguments)


def discovery_confirm_mapping_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or set(arguments) != {"candidate_number"}:
        raise AgentToolError("必须且只能提供 candidate_number")
    value = arguments["candidate_number"]
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
        raise AgentToolError("candidate_number 必须是 1 到 5 的整数")
    return {"candidate_number": int(value)}


def _feature_disabled() -> ToolResult:
    return ToolResult(False, "disabled", "影视探索功能当前已关闭", error="请先启用影视探索。")


def get_discovery_detail(arguments: dict[str, Any], _context: ToolContext) -> ToolResult:
    if not config.get_bool("DISCOVERY_ENABLED"):
        return _feature_disabled()
    card = get_discovery_service().get_detail(
        arguments["provider"], arguments["media_type"], arguments["external_id"]
    )
    if card is None:
        return ToolResult(False, "not_found", "没有找到该影视详情", error="来源详情不存在。")
    rating = None
    try:
        rating = round(max(0.0, min(float(card.rating), 10.0)), 1) if card.rating is not None else None
    except (TypeError, ValueError, OverflowError):
        rating = None
    return ToolResult(
        True,
        "completed",
        f"已读取《{_safe(card.title, 160) or '未命名条目'}》的发现详情",
        data={
            "provider": card.provider,
            "media_type": card.media_type,
            "title": _safe(card.title, 160) or "未命名条目",
            "original_title": _safe(card.original_title, 160),
            "year": _safe(card.year, 12),
            "release_date": _safe(card.release_date, 24),
            "rating": rating,
            "rating_source": _safe(card.rating_source, 30),
            "overview": _safe(card.overview, 500),
            "mapping_confirmed": bool(
                card.provider == "tmdb" or (
                    (row := db.get_media_external_id(card.provider, card.external_id, card.media_type))
                    and bool(row["confirmed"])
                )
            ),
        },
        evidence=[Evidence(
            "discovery_detail",
            "只读取来源详情与映射确认状态；未保存映射、收藏、订阅或下载任务。",
            _now(),
        )],
    )


def configure_discovery_mapping_context(
    repository: AgentSessionContextRepository | None,
) -> None:
    """配置跨 Worker 的映射候选短期上下文仓储。"""
    global _repository
    _repository = repository


def reset_discovery_mapping_context_for_tests() -> None:
    global _repository
    with _lock:
        _recent.clear()
    _repository = None


def clear_discovery_mapping_context(
    *, owner: str, delete_persisted: bool = True,
) -> bool:
    owner_key = str(owner or "").strip()
    if not owner_key:
        return False
    with _lock:
        removed = _recent.pop(owner_key, None) is not None
    if delete_persisted and _repository is not None:
        try:
            removed = bool(_repository.delete_latest(
                owner=owner_key, context_type=_CONTEXT_TYPE,
            )) or removed
        except Exception as exc:
            logger.warning(
                "Agent 发现映射上下文清理失败 type=%s", type(exc).__name__
            )
    return removed


def _snapshot_payload(snapshot: _MappingSnapshot) -> dict[str, Any]:
    return {
        "provider": snapshot.provider,
        "external_id": snapshot.external_id,
        "media_type": snapshot.media_type,
        "title": snapshot.title,
        "year": snapshot.year,
        "candidates": [dict(item) for item in snapshot.candidates[:5]],
    }


def _snapshot_from_payload(
    owner: str, payload: dict[str, Any], *, generation: int = 0, revision: int = 0,
) -> _MappingSnapshot | None:
    try:
        provider = str(payload.get("provider") or "").strip().casefold()
        external_id = str(payload.get("external_id") or "").strip()
        media_type = str(payload.get("media_type") or "").strip().casefold()
        raw_candidates = payload.get("candidates")
        if (
            provider not in _ALLOWED_PROVIDERS
            or media_type not in _ALLOWED_MEDIA_TYPES
            or not _PUBLIC_ID_RE.fullmatch(external_id)
            or not isinstance(raw_candidates, list)
        ):
            return None
        candidates: list[dict[str, Any]] = []
        for raw in raw_candidates[:5]:
            if not isinstance(raw, dict):
                return None
            number = raw.get("candidate_number")
            tmdb_id = str(raw.get("tmdb_id") or "").strip()
            candidate_type = str(raw.get("media_type") or "").strip().casefold()
            if (
                isinstance(number, bool)
                or not isinstance(number, int)
                or number != len(candidates) + 1
                or not tmdb_id.isdigit()
                or candidate_type not in _ALLOWED_MEDIA_TYPES
            ):
                return None
            score = float(raw.get("score") or 0)
            if not 0 <= score <= 1:
                return None
            candidates.append({
                "candidate_number": number,
                "tmdb_id": tmdb_id,
                "title": _safe(raw.get("title"), 160) or "未命名候选",
                "year": _safe(raw.get("year"), 12),
                "media_type": candidate_type,
                "score": round(score, 3),
            })
        return _MappingSnapshot(
            owner=str(owner),
            provider=provider,
            external_id=external_id,
            media_type=media_type,
            title=_safe(payload.get("title"), 160),
            year=_safe(payload.get("year"), 12),
            candidates=candidates,
            created_at=time.monotonic(),
            generation=max(0, int(generation or 0)),
            revision=max(0, int(revision or 0)),
        )
    except (TypeError, ValueError, OverflowError):
        return None


def _begin_snapshot(owner: str) -> AgentContextWriteGuard:
    if _repository is None:
        return AgentContextWriteGuard(generation=0, revision=0)
    begin = getattr(_repository, "begin_context", None)
    if not callable(begin):
        return AgentContextWriteGuard(generation=0, revision=0)
    return begin(owner=owner, context_type=_CONTEXT_TYPE)


def _store_snapshot(snapshot: _MappingSnapshot) -> bool:
    snapshot.created_at = time.monotonic()
    if _repository is not None:
        try:
            guarded = getattr(_repository, "replace_latest_guarded", None)
            if callable(guarded):
                if snapshot.generation <= 0:
                    with _lock:
                        _recent.pop(snapshot.owner, None)
                    return False
                persisted = guarded(
                    owner=snapshot.owner,
                    context_type=_CONTEXT_TYPE,
                    payload=_snapshot_payload(snapshot),
                    expires_at=time.time() + _TTL_SECONDS,
                    guard=AgentContextWriteGuard(
                        generation=snapshot.generation,
                        revision=snapshot.revision,
                    ),
                )
                if persisted is None:
                    with _lock:
                        _recent.pop(snapshot.owner, None)
                    return False
                snapshot.generation = persisted.generation
                snapshot.revision = persisted.revision
            else:
                _repository.replace_latest(
                    owner=snapshot.owner,
                    context_type=_CONTEXT_TYPE,
                    payload=_snapshot_payload(snapshot),
                    expires_at=time.time() + _TTL_SECONDS,
                )
        except Exception as exc:
            logger.warning(
                "Agent 发现映射上下文持久化失败 type=%s", type(exc).__name__
            )
            with _lock:
                _recent.pop(snapshot.owner, None)
            return False
    with _lock:
        expired = [owner for owner, item in _recent.items() if time.monotonic() - item.created_at > _TTL_SECONDS]
        for owner in expired:
            _recent.pop(owner, None)
        _recent[snapshot.owner] = snapshot
    return True


def _consume_snapshot(snapshot: _MappingSnapshot) -> bool:
    if _repository is not None:
        consume = getattr(_repository, "consume_latest_guarded", None)
        try:
            if callable(consume) and snapshot.generation > 0 and snapshot.revision > 0:
                consumed = bool(consume(
                    owner=snapshot.owner,
                    context_type=_CONTEXT_TYPE,
                    guard=AgentContextWriteGuard(
                        generation=snapshot.generation,
                        revision=snapshot.revision,
                    ),
                ))
            elif not callable(consume):
                consumed = bool(_repository.delete_latest(
                    owner=snapshot.owner, context_type=_CONTEXT_TYPE,
                ))
            else:
                consumed = False
        except Exception as exc:
            logger.warning(
                "Agent 发现映射上下文消费失败 type=%s", type(exc).__name__
            )
            return False
        if not consumed:
            return False
    with _lock:
        _recent.pop(snapshot.owner, None)
    return True


def _get_snapshot(owner: str) -> _MappingSnapshot | None:
    owner_key = str(owner or "").strip()
    if not owner_key:
        return None
    if _repository is not None:
        try:
            persisted = _repository.get_latest(
                owner=owner_key, context_type=_CONTEXT_TYPE, now=time.time(),
            )
        except Exception as exc:
            logger.warning(
                "Agent 发现映射上下文恢复失败 type=%s", type(exc).__name__
            )
            with _lock:
                _recent.pop(owner_key, None)
            return None
        if persisted is None:
            with _lock:
                _recent.pop(owner_key, None)
            return None
        if callable(getattr(_repository, "replace_latest_guarded", None)) and (
            persisted.generation <= 0 or persisted.revision <= 0
        ):
            with _lock:
                _recent.pop(owner_key, None)
            return None
        restored = _snapshot_from_payload(
            owner_key, persisted.payload,
            generation=persisted.generation, revision=persisted.revision,
        )
        if restored is None:
            with _lock:
                _recent.pop(owner_key, None)
            return None
        with _lock:
            _recent[owner_key] = restored
        return restored
    with _lock:
        item = _recent.get(owner_key)
        if item is None or time.monotonic() - item.created_at > _TTL_SECONDS:
            _recent.pop(owner_key, None)
            return None
        return item


def get_discovery_mapping_candidates(arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    if not context.owner:
        raise AgentToolError("映射候选需要已登录会话", code="precondition_failed")
    if not config.get_bool("DISCOVERY_ENABLED"):
        return _feature_disabled()
    try:
        guard = _begin_snapshot(context.owner)
    except Exception as exc:
        logger.warning("Agent 发现映射上下文初始化失败 type=%s", type(exc).__name__)
        raise AgentToolError("映射候选上下文暂时不可用", code="precondition_failed") from exc
    service = get_discovery_service()
    card = service.get_detail(arguments["provider"], arguments["media_type"], arguments["external_id"])
    if card is None:
        return ToolResult(False, "not_found", "没有找到该影视详情", error="来源详情不存在。")
    result = service.lookup_tmdb_mapping(
        card.provider, card.external_id, card.media_type, card.title, card.year
    )
    raw_candidates = result.get("candidates") if isinstance(result.get("candidates"), list) else []
    candidates: list[dict[str, Any]] = []
    for raw in raw_candidates[:5]:
        if not isinstance(raw, dict):
            continue
        tmdb_id = str(raw.get("tmdb_id") or "").strip()
        if not tmdb_id.isdigit():
            continue
        try:
            score = round(max(0.0, min(float(raw.get("score") or 0), 1.0)), 3)
        except (TypeError, ValueError, OverflowError):
            score = 0.0
        candidates.append({
            "candidate_number": len(candidates) + 1,
            "tmdb_id": tmdb_id,
            "title": _safe(raw.get("title"), 160) or "未命名候选",
            "year": _safe(raw.get("year"), 12),
            "media_type": str(raw.get("media_type") or card.media_type) if str(raw.get("media_type") or card.media_type) in _ALLOWED_MEDIA_TYPES else card.media_type,
            "score": score,
        })
    stored = _store_snapshot(_MappingSnapshot(
        owner=context.owner, provider=card.provider, external_id=card.external_id,
        media_type=card.media_type, title=_safe(card.title, 160), year=_safe(card.year, 12),
        candidates=candidates, created_at=time.monotonic(),
        generation=guard.generation, revision=guard.revision,
    ))
    if not stored:
        raise AgentToolError("本次映射候选已被更新请求取代，请重新查询", code="precondition_failed")
    confirmed = bool(result.get("confirmed"))
    return ToolResult(
        True,
        "completed" if confirmed or candidates else "empty",
        ("该条目已有确认过的 TMDB 映射" if confirmed else f"找到 {len(candidates)} 个 TMDB 映射候选"),
        data={
            "source_title": _safe(card.title, 160) or "未命名条目",
            "provider": card.provider,
            "media_type": card.media_type,
            "mapping_confirmed": confirmed,
            "candidates": [{k: v for k, v in item.items() if k != "tmdb_id"} for item in candidates],
            "candidate_count": len(candidates),
            "limits": {"max_candidates": 5, "ttl_seconds": int(_TTL_SECONDS)},
        },
        evidence=[Evidence(
            "discovery_mapping",
            "只读查询映射与 TMDB 候选；未写入 media_external_ids。候选内部 TMDB ID 仅保存在当前会话短期上下文。",
            _now(),
        )],
        suggestions=([] if confirmed else ["如需保存，请回复：确认第 1 个映射。"] if candidates else []),
    )


def _current_mapping_state(snapshot: _MappingSnapshot) -> dict[str, Any] | None:
    existing = db.get_media_external_id(
        snapshot.provider, snapshot.external_id, snapshot.media_type
    )
    return None if existing is None else {
        "tmdb_id": str(existing["tmdb_id"] or ""),
        "confirmed": bool(existing["confirmed"]),
        "version": max(0, int(existing["version"] or 0)),
        "updated_at": str(existing["updated_at"] or ""),
    }


def _mapping_context(
    snapshot: _MappingSnapshot,
    candidate: dict[str, Any],
    *,
    existing_state: dict[str, Any] | None = None,
    state_loaded: bool = False,
) -> str:
    if not state_loaded:
        existing_state = _current_mapping_state(snapshot)
    return confirmation_context_fingerprint(
        {
            "source": [snapshot.provider, snapshot.external_id, snapshot.media_type, snapshot.title, snapshot.year],
            "candidate": candidate,
            "existing": existing_state,
        },
        domain="discovery-confirm-mapping",
    )


def prepare_confirm_discovery_mapping(
    arguments: dict[str, Any], context: ToolContext
) -> tuple[ToolResult, str]:
    snapshot = _get_snapshot(context.owner)
    if snapshot is None:
        raise AgentToolError("最近映射候选不存在或已过期，请重新查询", code="precondition_failed")
    candidate = next((item for item in snapshot.candidates if item["candidate_number"] == arguments["candidate_number"]), None)
    if candidate is None:
        raise AgentToolError("映射候选序号不存在", code="precondition_failed")
    service = get_discovery_service()
    try:
        match = service.verify_tmdb_mapping_candidate(candidate["tmdb_id"], snapshot.media_type)
    except (TypeError, ValueError):
        raise AgentToolError("所选 TMDB 候选当前无法核验", code="precondition_failed")
    verified = dict(candidate)
    verified["title"] = _safe(match.get("title"), 160) or candidate["title"]
    verified["year"] = _safe(match.get("year"), 12) or candidate["year"]
    snapshot.candidates = [verified if item["candidate_number"] == arguments["candidate_number"] else item for item in snapshot.candidates]
    if not _store_snapshot(snapshot):
        raise AgentToolError("映射候选已变化，请重新查询", code="confirmation_stale")
    fingerprint = _mapping_context(snapshot, verified)
    return ToolResult(
        True,
        "confirmation_required",
        f"确认后将《{snapshot.title or '该条目'}》映射到《{verified['title']}》",
        data={
            "source_title": snapshot.title or "未命名条目",
            "candidate_number": verified["candidate_number"],
            "candidate_title": verified["title"],
            "candidate_year": verified["year"],
            "effects": [
                "会保存一条确认过的来源到 TMDB 身份映射。",
                "不会加入收藏、创建订阅、搜索资源或下载。",
            ],
        },
        evidence=[Evidence("tmdb_detail", "已重新读取并核验所选 TMDB 候选身份。", _now())],
    ), fingerprint


def confirm_discovery_mapping_confirmed(
    arguments: dict[str, Any], expected_context: str, context: ToolContext
) -> ToolResult:
    snapshot = _get_snapshot(context.owner)
    if snapshot is None:
        raise AgentToolError("映射候选已过期，请重新查询", code="confirmation_stale")
    candidate = next((item for item in snapshot.candidates if item["candidate_number"] == arguments["candidate_number"]), None)
    existing_state = _current_mapping_state(snapshot)
    if candidate is None or _mapping_context(
        snapshot, candidate, existing_state=existing_state, state_loaded=True
    ) != str(expected_context or ""):
        raise AgentToolError("来源、候选或现有映射已变化，请重新预检", code="confirmation_stale")
    if not _consume_snapshot(snapshot):
        raise AgentToolError("映射候选已变化或已被消费，请重新查询", code="confirmation_stale")
    saved = get_discovery_service().confirm_tmdb_mapping_if_unchanged(
        snapshot.provider,
        snapshot.external_id,
        snapshot.media_type,
        candidate["tmdb_id"],
        existing_state,
    )
    if saved is None:
        raise AgentToolError("现有映射在保存时发生变化，请重新预检", code="confirmation_stale")
    return ToolResult(
        True,
        "completed",
        f"已确认《{snapshot.title or '该条目'}》的 TMDB 映射",
        data={
            "affected": 1,
            "provider": snapshot.provider,
            "media_type": snapshot.media_type,
            "candidate_number": candidate["candidate_number"],
            "candidate_title": candidate["title"],
            "mapping_confirmed": True,
        },
        evidence=[Evidence("media_external_ids", "已保存经过 TMDB 详情核验的确认映射。", _now())],
    )
