"""会话绑定的短期缺集资源推荐安全投影。"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from datetime import date
import re
import threading
import time
import unicodedata
from typing import Any, Callable

from app.agent.models import ToolResult


_RESULT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_ALLOWED_CONFIDENCE = {"high", "medium", "low"}
_ALLOWED_MATCH = {"exact_episode", "episode_pack", "season_pack", "unknown"}
_ALLOWED_DOWNLOAD_STATE = {"ready", "resolvable"}
_ALLOWED_DOWNLOAD_KINDS = {"magnet", "torrent"}
_MAX_CANDIDATES = 12


class RecentResourceCandidateStore:
    """保存最近一次缺集资源搜索的安全、短期、owner 绑定候选。"""

    def __init__(
        self,
        *,
        ttl_seconds: int = 600,
        max_entries: int = 256,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self._clock = clock
        self._lock = threading.RLock()
        self._entries: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()

    def capture(self, *, owner: str, result: ToolResult) -> None:
        owner_key = str(owner or "").strip()
        if not owner_key:
            return
        snapshot = _safe_snapshot(result)
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            self._entries.pop(owner_key, None)
            self._entries[owner_key] = (now + self.ttl_seconds, snapshot)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def get(self, *, owner: str) -> dict[str, Any] | None:
        owner_key = str(owner or "").strip()
        if not owner_key:
            return None
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            entry = self._entries.get(owner_key)
            if entry is None:
                return None
            self._entries.move_to_end(owner_key)
            return deepcopy(entry[1])

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()

    def clear_owner(self, *, owner: str) -> bool:
        owner_key = str(owner or "").strip()
        if not owner_key:
            return False
        with self._lock:
            return self._entries.pop(owner_key, None) is not None

    def _prune_locked(self, now: float) -> None:
        expired = [owner for owner, (expires_at, _) in self._entries.items() if expires_at <= now]
        for owner in expired:
            self._entries.pop(owner, None)


def _safe_snapshot(result: ToolResult) -> dict[str, Any]:
    data = result.data if isinstance(result.data, dict) else {}
    generic_items = data.get("items")
    if isinstance(generic_items, list):
        candidates: list[dict[str, Any]] = []
        seen_result_ids: set[str] = set()
        for raw in generic_items[:_MAX_CANDIDATES]:
            projected = _safe_generic_candidate(raw)
            if not projected or projected["result_id"] in seen_result_ids:
                continue
            seen_result_ids.add(projected["result_id"])
            projected["position"] = len(candidates) + 1
            candidates.append(projected)
        return {
            "search_status": str(result.status or "")[:40],
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
        "search_status": str(result.status or "")[:40],
        "candidates": candidates,
    }


def _safe_generic_candidate(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result_id = str(value.get("result_id") or "").strip()
    title = _safe_text(value.get("title"), 300)
    download_state = str(value.get("download_state") or "").strip().lower()
    raw_kinds = value.get("download_kinds")
    download_kinds = (
        {str(item or "").strip().lower() for item in raw_kinds}
        if isinstance(raw_kinds, list)
        else set()
    )
    if (
        not _RESULT_ID_PATTERN.fullmatch(result_id)
        or not title
        or download_state not in _ALLOWED_DOWNLOAD_STATE
        or not download_kinds.intersection(_ALLOWED_DOWNLOAD_KINDS)
    ):
        return None
    return {
        "result_id": result_id,
        "title": title,
        "site_id": _safe_text(value.get("site_id"), 32),
        "site_name": _safe_text(value.get("site_name"), 80),
        "size_text": _safe_text(value.get("size_text"), 32),
        "download_state": download_state,
        "_verification_context": None,
    }


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
        "_verification_context": verification_context,
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
