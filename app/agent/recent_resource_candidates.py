"""会话绑定的短期缺集资源推荐安全投影。"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from datetime import date
import logging
import re
import secrets
import threading
import time
import unicodedata
from typing import Any, Callable

from app.agent.models import ToolResult
from app.agent.session_context import AgentSessionContextRepository


_RESULT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_SEARCH_ID_PATTERN = re.compile(r"^rs_[A-Za-z0-9_-]{16,64}$")
_ALLOWED_CONFIDENCE = {"high", "medium", "low"}
_ALLOWED_MATCH = {"exact_episode", "episode_pack", "season_pack", "unknown"}
_ALLOWED_DOWNLOAD_STATE = {"ready", "resolvable"}
_ALLOWED_DOWNLOAD_KINDS = {"magnet", "torrent"}
_MAX_CANDIDATES = 12
_CONTEXT_TYPE = "resource_candidates"
logger = logging.getLogger(__name__)


def new_resource_search_id() -> str:
    """签发不可猜测的公开搜索快照标识。"""
    return f"rs_{secrets.token_urlsafe(16)}"


def normalize_resource_search_id(value: Any) -> str:
    """仅接受本服务签发的搜索快照标识；空值表示未指定。"""
    search_id = str(value or "").strip()
    return search_id if _SEARCH_ID_PATTERN.fullmatch(search_id) else ""


def safe_resource_snapshot(
    result: ToolResult, *, search_id: str
) -> dict[str, Any]:
    """返回与持久候选仓相同的脱敏快照，供单次请求内按序号续接。"""
    snapshot = deepcopy(_safe_snapshot(result))
    normalized_search_id = normalize_resource_search_id(search_id)
    if not normalized_search_id:
        raise ValueError("invalid resource search id")
    snapshot["search_id"] = normalized_search_id
    return snapshot


class RecentResourceCandidateStore:
    """保存最近一次缺集资源搜索的安全、短期、owner 绑定候选。"""

    def __init__(
        self,
        *,
        ttl_seconds: int = 600,
        max_entries: int = 256,
        max_snapshots_per_owner: int = 5,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        repository: AgentSessionContextRepository | None = None,
    ) -> None:
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self.max_snapshots_per_owner = max(1, min(int(max_snapshots_per_owner), 16))
        self._clock = clock
        self._wall_clock = wall_clock
        self._repository = repository
        self._lock = threading.RLock()
        self._owner_locks = tuple(threading.RLock() for _ in range(64))
        self._entries: OrderedDict[str, list[tuple[float, dict[str, Any]]]] = OrderedDict()

    def capture(
        self, *, owner: str, result: ToolResult, search_id: str = ""
    ) -> str:
        owner_key = str(owner or "").strip()
        if not owner_key:
            return ""
        effective_search_id = search_id or new_resource_search_id()
        snapshot = safe_resource_snapshot(result, search_id=effective_search_id)
        with self._owner_lock(owner_key):
            now = self._clock()
            with self._lock:
                self._prune_locked(now)
                items = self._entries.pop(owner_key, [])
                items.insert(0, (now + self.ttl_seconds, snapshot))
                self._entries[owner_key] = items[: self.max_snapshots_per_owner]
                while len(self._entries) > self.max_entries:
                    self._entries.popitem(last=False)
            if self._repository is not None:
                try:
                    self._repository.append_snapshot(
                        owner=owner_key,
                        context_type=_CONTEXT_TYPE,
                        payload=snapshot,
                        expires_at=self._wall_clock() + self.ttl_seconds,
                        max_items=self.max_snapshots_per_owner,
                    )
                except Exception as exc:
                    logger.warning(
                        "Agent 资源候选上下文持久化失败 type=%s",
                        type(exc).__name__,
                    )
        return effective_search_id

    def get(self, *, owner: str, search_id: str = "") -> dict[str, Any] | None:
        owner_key = str(owner or "").strip()
        if not owner_key:
            return None
        with self._owner_lock(owner_key):
            now = self._clock()
            with self._lock:
                self._prune_locked(now)
                entries = self._entries.get(owner_key)
                if entries:
                    self._entries.move_to_end(owner_key)
                    selected = next(
                        (snapshot for _expires, snapshot in entries if snapshot.get("search_id") == search_id),
                        entries[0][1] if not search_id else None,
                    )
                    return deepcopy(selected) if selected is not None else None
            return self._restore(owner_key=owner_key, now=now, search_id=search_id)

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()

    def clear_owner(self, *, owner: str) -> bool:
        owner_key = str(owner or "").strip()
        if not owner_key:
            return False
        removed = False
        with self._owner_lock(owner_key):
            with self._lock:
                removed = self._entries.pop(owner_key, None) is not None
            if self._repository is not None:
                try:
                    removed = bool(self._repository.delete_latest(
                        owner=owner_key, context_type=_CONTEXT_TYPE
                    )) or removed
                except Exception as exc:
                    logger.warning(
                        "Agent 资源候选上下文清理失败 type=%s",
                        type(exc).__name__,
                    )
        return removed

    def _owner_lock(self, owner_key: str) -> threading.RLock:
        return self._owner_locks[hash(owner_key) % len(self._owner_locks)]

    def _prune_locked(self, now: float) -> None:
        for owner, entries in list(self._entries.items()):
            active = [entry for entry in entries if entry[0] > now]
            if active:
                self._entries[owner] = active
            else:
                self._entries.pop(owner, None)

    def _restore(self, *, owner_key: str, now: float, search_id: str = "") -> dict[str, Any] | None:
        if self._repository is None:
            return None
        wall_now = self._wall_clock()
        try:
            persisted_items = self._repository.list_snapshots(
                owner=owner_key,
                context_type=_CONTEXT_TYPE,
                now=wall_now,
                limit=self.max_snapshots_per_owner,
            )
        except Exception as exc:
            logger.warning(
                "Agent 资源候选上下文恢复失败 type=%s", type(exc).__name__
            )
            return None
        if not persisted_items:
            return None
        restored: list[tuple[float, dict[str, Any]]] = []
        for persisted in persisted_items:
            snapshot = validate_safe_resource_snapshot(persisted.payload)
            remaining = persisted.expires_at - wall_now
            if snapshot is not None and remaining > 0:
                restored.append((now + min(float(self.ttl_seconds), remaining), snapshot))
        if not restored:
            return None
        with self._lock:
            self._prune_locked(now)
            self._entries[owner_key] = restored
            self._entries.move_to_end(owner_key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
        selected = next(
            (snapshot for _expires, snapshot in restored if snapshot.get("search_id") == search_id),
            restored[0][1] if not search_id else None,
        )
        return deepcopy(selected) if selected is not None else None



def _safe_snapshot(result: ToolResult) -> dict[str, Any]:
    data = result.data if isinstance(result.data, dict) else {}
    generic_items = data.get("items")
    if isinstance(generic_items, list):
        candidates: list[dict[str, Any]] = []
        seen_result_ids: set[str] = set()
        for raw in generic_items:
            projected = _safe_generic_candidate(raw)
            if not projected or projected["result_id"] in seen_result_ids:
                continue
            seen_result_ids.add(projected["result_id"])
            projected["position"] = len(candidates) + 1
            candidates.append(projected)
            if len(candidates) >= _MAX_CANDIDATES:
                break
        return {
            "search_status": _safe_text(str(result.status or ""), 40),
            "candidates": candidates,
        }

    search_entries: list[tuple[int, int, str, dict[str, Any], dict[str, Any] | None]] = []

    search = data.get("search")
    if isinstance(search, dict):
        verification = data.get("verification") if isinstance(data.get("verification"), dict) else {}
        season = _safe_positive_int(verification.get("season"), maximum=100)
        episode = _safe_positive_int(verification.get("episode"), maximum=1000)
        if season and episode:
            search_entries.append((
                season, episode, f"S{season:02d}E{episode:02d}", search,
                _safe_verification_context(verification, season=season, episode=episode),
            ))

    episodes = data.get("episodes")
    if isinstance(episodes, list):
        for entry in episodes[:3]:
            if not isinstance(entry, dict):
                continue
            season = _safe_positive_int(entry.get("season"), maximum=100)
            episode = _safe_positive_int(entry.get("episode"), maximum=1000)
            search = entry.get("search")
            if not season or not episode or not isinstance(search, dict):
                continue
            label = _safe_text(entry.get("episode_label"), 24) or f"S{season:02d}E{episode:02d}"
            verification = data.get("verification") if isinstance(data.get("verification"), dict) else {}
            search_entries.append((
                season, episode, label, search,
                _safe_verification_context(verification, season=season, episode=episode),
            ))

    candidates: list[dict[str, Any]] = []
    seen_result_ids: set[str] = set()
    for season, episode, episode_label, search_data, verification_context in search_entries:
        recommendation = search_data.get("recommendation")
        if not isinstance(recommendation, dict):
            continue
        raw_candidates = [recommendation.get("selected")]
        alternatives = recommendation.get("alternatives")
        if isinstance(alternatives, list):
            raw_candidates.extend(alternatives[:3])
        for raw in raw_candidates:
            if len(candidates) >= _MAX_CANDIDATES:
                break
            projected = _safe_candidate(
                raw,
                season=season,
                episode=episode,
                episode_label=episode_label,
                verification_context=verification_context,
            )
            if not projected:
                continue
            result_id = projected["result_id"]
            if result_id in seen_result_ids:
                continue
            seen_result_ids.add(result_id)
            projected["position"] = len(candidates) + 1
            candidates.append(projected)

    return {
        "search_status": _safe_text(str(result.status or ""), 40),
        "candidates": candidates,
    }


def _safe_generic_candidate(
    value: Any, *, require_download_kinds: bool = True
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result_id = str(value.get("result_id") or "").strip()
    title = _safe_text(value.get("title"), 300)
    download_state = str(value.get("download_state") or "").strip().lower()
    raw_kinds = value.get("download_kinds")
    download_kinds = sorted(
        {
            str(item or "").strip().lower()
            for item in raw_kinds
            if str(item or "").strip().lower() in _ALLOWED_DOWNLOAD_KINDS
        }
    ) if isinstance(raw_kinds, list) else []
    if (
        not _RESULT_ID_PATTERN.fullmatch(result_id)
        or not title
        or download_state not in _ALLOWED_DOWNLOAD_STATE
        or (require_download_kinds and not download_kinds)
    ):
        return None
    subscription_number = _safe_positive_int(
        value.get("subscription_number"), maximum=2_147_483_647
    )
    return {
        "result_id": result_id,
        "title": title,
        "site_id": _safe_text(value.get("site_id"), 32),
        "site_name": _safe_text(value.get("site_name"), 80),
        "size_text": _safe_text(value.get("size_text"), 32),
        "download_state": download_state,
        "download_kinds": download_kinds,
        "media_title": _safe_text(value.get("media_title"), 160),
        "episode_label": _safe_text(value.get("episode_label"), 24),
        "subscription_number": subscription_number,
        "_verification_context": None,
    }


_ALLOWED_QUALITY_TAGS = frozenset({
    "resolution", "media", "video_codec", "effect", "audio",
})


def _safe_quality_messages(value: Any, *, maximum: int) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for raw in value[:maximum]:
        text = _safe_text(raw, 120)
        if text and text not in result:
            result.append(text)
    return result


def _safe_quality_tags(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key in sorted(_ALLOWED_QUALITY_TAGS):
        text = _safe_text(value.get(key), 64)
        if text:
            result[key] = text
    return result


def _safe_candidate(
    value: Any,
    *,
    season: int,
    episode: int,
    episode_label: str,
    verification_context: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result_id = str(value.get("result_id") or "").strip()
    title = _safe_text(value.get("title"), 300)
    download_state = str(value.get("download_state") or "").strip()
    confidence = str(value.get("confidence") or "").strip()
    match = str(value.get("match") or "").strip()
    rank = _safe_positive_int(value.get("rank"), maximum=50)
    score = _safe_int(value.get("score"), minimum=-1000, maximum=1000)
    if (
        not _RESULT_ID_PATTERN.fullmatch(result_id)
        or not title
        or download_state not in _ALLOWED_DOWNLOAD_STATE
        or confidence not in _ALLOWED_CONFIDENCE
        or match not in _ALLOWED_MATCH
        or rank is None
        or score is None
    ):
        return None
    return {
        "season": season,
        "episode": episode,
        "episode_label": episode_label,
        "result_id": result_id,
        "title": title,
        "site_id": _safe_text(value.get("site_id"), 32),
        "site_name": _safe_text(value.get("site_name"), 80),
        "rank": rank,
        "score": score,
        "confidence": confidence,
        "match": match,
        "download_state": download_state,
        "reasons": _safe_quality_messages(value.get("reasons"), maximum=6),
        "warnings": _safe_quality_messages(value.get("warnings"), maximum=4),
        "tags": _safe_quality_tags(value.get("tags")),
        "_verification_context": verification_context,
    }


_GENERIC_PERSISTED_KEYS = frozenset({
    "position", "result_id", "title", "site_id", "site_name", "size_text",
    "download_state", "_verification_context",
    "download_kinds", "media_title", "episode_label", "subscription_number",
})
_EPISODIC_PERSISTED_KEYS = frozenset({
    "position", "season", "episode", "episode_label", "result_id", "title",
    "site_id", "site_name", "rank", "score", "confidence", "match",
    "download_state", "_verification_context",
    "reasons", "warnings", "tags",
})


def validate_safe_resource_snapshot(value: Any) -> dict[str, Any] | None:
    """严格验证持久化资源候选投影，拒绝额外字段或被篡改的句柄。"""
    if not isinstance(value, dict) or set(value) != {
        "search_id",
        "search_status",
        "candidates",
    }:
        return None
    search_id = normalize_resource_search_id(value.get("search_id"))
    if not search_id:
        return None
    search_status = _safe_text(value.get("search_status"), 40)
    raw_candidates = value.get("candidates")
    if (
        search_status != value.get("search_status")
        or not isinstance(raw_candidates, list)
        or len(raw_candidates) > _MAX_CANDIDATES
    ):
        return None
    candidates: list[dict[str, Any]] = []
    seen_result_ids: set[str] = set()
    for expected_position, raw in enumerate(raw_candidates, start=1):
        if not isinstance(raw, dict):
            return None
        keys = frozenset(raw)
        if keys == _GENERIC_PERSISTED_KEYS:
            result_id = str(raw.get("result_id") or "").strip()
            projected = _safe_generic_candidate(
                raw,
                require_download_kinds=True,
            )
            if projected is None:
                return None
            projected["position"] = expected_position
            if raw != projected:
                return None
        elif keys == _EPISODIC_PERSISTED_KEYS:
            season = _safe_positive_int(raw.get("season"), maximum=100)
            episode = _safe_positive_int(raw.get("episode"), maximum=1000)
            if season is None or episode is None:
                return None
            verification = raw.get("_verification_context")
            if verification is not None:
                if not isinstance(verification, dict) or set(verification) not in ({
                    "title", "tmdb_id", "season", "episode", "as_of"
                }, {
                    "title", "tmdb_id", "season", "episode", "as_of", "library_name"
                }):
                    return None
                safe_verification = _safe_verification_context(
                    verification, season=season, episode=episode
                )
                if safe_verification != verification:
                    return None
            else:
                safe_verification = None
            projected = _safe_candidate(
                raw,
                season=season,
                episode=episode,
                episode_label=_safe_text(raw.get("episode_label"), 24),
                verification_context=safe_verification,
            )
            if projected is None:
                return None
            projected["position"] = expected_position
            if raw != projected:
                return None
            result_id = projected["result_id"]
        else:
            return None
        if result_id in seen_result_ids:
            return None
        seen_result_ids.add(result_id)
        candidates.append(projected)
    return {
        "search_id": search_id,
        "search_status": search_status,
        "candidates": candidates,
    }


def public_candidate_projection(value: Any) -> dict[str, Any]:
    """移除仅供服务端确认续接使用的内部字段。"""
    if not isinstance(value, dict):
        return {}
    return {key: deepcopy(item) for key, item in value.items() if not str(key).startswith("_")}


def _safe_verification_context(
    value: Any,
    *,
    season: int,
    episode: int,
) -> dict[str, Any] | None:
    if not isinstance(value, dict) or value.get("verified_missing") is not True:
        return None
    title = _safe_text(value.get("title"), 120)
    tmdb_id = str(value.get("tmdb_id") or "").strip()
    as_of = str(value.get("as_of") or "").strip()
    if not title or not tmdb_id.isascii() or not tmdb_id.isdigit() or not 1 <= len(tmdb_id) <= 10:
        return None
    try:
        parsed_as_of = date.fromisoformat(as_of)
    except ValueError:
        return None
    if parsed_as_of > date.today():
        return None
    context = {
        "title": title,
        "tmdb_id": tmdb_id,
        "season": season,
        "episode": episode,
        "as_of": parsed_as_of.isoformat(),
    }
    library_name = _safe_text(value.get("library_name"), 80)
    if library_name:
        context["library_name"] = library_name
    return context


def _safe_text(value: Any, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(unicodedata.normalize("NFKC", value).split())
    if not text or any(unicodedata.category(char).startswith("C") for char in text):
        return ""
    return text[:maximum]


def _safe_positive_int(value: Any, *, maximum: int) -> int | None:
    parsed = _safe_int(value, minimum=1, maximum=maximum)
    return parsed if parsed is not None else None


def _safe_int(value: Any, *, minimum: int, maximum: int) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if minimum <= parsed <= maximum else None
