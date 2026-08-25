"""本地整理识别摘要的持久化、聚合与历史兼容。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from app.modules.recognition.resolver import _resolve_explicit_tmdb_marker

SUMMARY_SCHEMA_VERSION = 1
_VALID_MEDIA_TYPES = {"movie", "tv"}


def _text(value: object) -> str:
    return str(value or "").strip()


def _optional_int(value: object, *, minimum: int = 0) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= minimum else None


def _identity_key(item: Mapping[str, Any]) -> tuple[str, str, str, str]:
    media_type = _text(item.get("media_type")).lower()
    tmdb_id = _text(item.get("tmdb_id"))
    title = _text(item.get("title"))
    year = _text(item.get("year"))
    if tmdb_id:
        return media_type, "tmdb", tmdb_id, ""
    return media_type, "title", title.casefold(), year


def build_recognition_summary(
    matches: Iterable[Mapping[str, Any]], *, source: str = "recognition",
) -> dict[str, Any]:
    """把逐文件识别结果聚合成适合持久化和详情展示的有限摘要。"""
    groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    order: list[tuple[str, str, str, str]] = []
    file_count = 0
    for raw in matches:
        title = _text(raw.get("title"))
        tmdb_id = _text(raw.get("tmdb_id"))
        media_type = _text(raw.get("media_type")).lower()
        if media_type not in _VALID_MEDIA_TYPES:
            media_type = ""
        if not title and not tmdb_id:
            continue
        item = {
            "tmdb_id": tmdb_id,
            "title": title,
            "year": _text(raw.get("year")),
            "media_type": media_type,
        }
        key = _identity_key(item)
        if key not in groups:
            groups[key] = {
                **item,
                "categories": [],
                "file_count": 0,
                "_positions": {},
            }
            order.append(key)
        group = groups[key]
        group["file_count"] += 1
        file_count += 1
        for field in ("tmdb_id", "title", "year", "media_type"):
            if not group.get(field) and item.get(field):
                group[field] = item[field]
        category = _text(raw.get("category"))
        if category and category not in group["categories"]:
            group["categories"].append(category)
        season = _optional_int(raw.get("season"), minimum=0)
        episode = _optional_int(raw.get("episode"), minimum=1)
        if season is not None or episode is not None:
            position_key = season
            position = group["_positions"].setdefault(
                position_key, {"season": season, "episodes": set(), "file_count": 0},
            )
            position["file_count"] += 1
            if episode is not None:
                position["episodes"].add(episode)

    media: list[dict[str, Any]] = []
    for key in order:
        group = groups[key]
        positions = []
        for position in sorted(
            group.pop("_positions").values(),
            key=lambda item: (-1 if item["season"] is None else item["season"]),
        ):
            positions.append({
                "season": position["season"],
                "episodes": sorted(position["episodes"]),
                "file_count": position["file_count"],
            })
        group["seasons"] = positions
        media.append(group)

    if not media:
        return {}
    return {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "source": _text(source) or "recognition",
        "status": "resolved" if len(media) == 1 else "multiple",
        "media_count": len(media),
        "file_count": file_count,
        "media": media,
    }


def serialize_recognition_summary(summary: Mapping[str, Any] | None) -> str:
    if not summary:
        return ""
    return json.dumps(
        dict(summary), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def parse_recognition_summary(raw: object) -> dict[str, Any]:
    try:
        payload = json.loads(_text(raw))
    except (TypeError, ValueError):
        return {}
    if not isinstance(payload, dict) or payload.get("schema_version") != SUMMARY_SCHEMA_VERSION:
        return {}
    raw_media = payload.get("media")
    if not isinstance(raw_media, list):
        return {}
    media: list[dict[str, Any]] = []
    for raw_item in raw_media:
        if not isinstance(raw_item, dict):
            continue
        title = _text(raw_item.get("title"))
        tmdb_id = _text(raw_item.get("tmdb_id"))
        if not title and not tmdb_id:
            continue
        media_type = _text(raw_item.get("media_type")).lower()
        if media_type not in _VALID_MEDIA_TYPES:
            media_type = ""
        categories = []
        for value in raw_item.get("categories") or []:
            category = _text(value)
            if category and category not in categories:
                categories.append(category)
        seasons = []
        for raw_position in raw_item.get("seasons") or []:
            if not isinstance(raw_position, dict):
                continue
            season = _optional_int(raw_position.get("season"), minimum=0)
            episodes = sorted({
                episode
                for value in raw_position.get("episodes") or []
                if (episode := _optional_int(value, minimum=1)) is not None
            })
            if season is None and not episodes:
                continue
            seasons.append({
                "season": season,
                "episodes": episodes,
                "file_count": _optional_int(raw_position.get("file_count"), minimum=0) or len(episodes),
            })
        seasons.sort(key=lambda item: -1 if item["season"] is None else item["season"])
        media.append({
            "tmdb_id": tmdb_id,
            "title": title,
            "year": _text(raw_item.get("year")),
            "media_type": media_type,
            "categories": categories,
            "file_count": _optional_int(raw_item.get("file_count"), minimum=0) or 0,
            "seasons": seasons,
        })
    if not media:
        return {}
    return {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "source": _text(payload.get("source")) or "recognition",
        "status": "resolved" if len(media) == 1 else "multiple",
        "media_count": len(media),
        "file_count": (
            _optional_int(payload.get("file_count"), minimum=0)
            or sum(item["file_count"] for item in media)
        ),
        "media": media,
    }


def summary_from_task(task: object) -> dict[str, Any]:
    title = _text(getattr(task, "title", ""))
    tmdb_id = _text(getattr(task, "tmdb_id", ""))
    media_type = _text(getattr(task, "media_type", "")).lower()
    if not title and not tmdb_id:
        return {}
    return build_recognition_summary(
        [{
            "tmdb_id": tmdb_id,
            "title": title,
            "year": _text(getattr(task, "year", "")),
            "media_type": media_type,
            "season": getattr(task, "season_override", None),
            "episode": getattr(task, "episode_override", None),
        }],
        source="manual_selection",
    )


def infer_recognition_summary(
    rows: Iterable[Mapping[str, Any]], *, scraper: object,
) -> dict[str, Any]:
    """仅凭带显式 TMDB 标记的历史目标路径做 fail-closed 推断。"""
    inferred: list[dict[str, Any]] = []
    for row in rows:
        if _text(row["role"] if "role" in row.keys() else "") != "video":
            continue
        target_raw = _text(row["target_path"] if "target_path" in row.keys() else "")
        if not target_raw:
            continue
        target = Path(target_raw)
        filename_id, filename_conflict = _resolve_explicit_tmdb_marker(target.name)
        parent_id, parent_conflict = _resolve_explicit_tmdb_marker(
            str(target.parent), nearest_first=True,
        )
        if filename_conflict or parent_conflict:
            continue
        if filename_id and parent_id and filename_id != parent_id:
            continue
        tmdb_id = filename_id or parent_id
        if not tmdb_id:
            continue
        try:
            parsed = scraper.parse_media(target.name, str(target.parent))
        except Exception:
            continue
        title = _text(getattr(parsed, "title", ""))
        if not title:
            continue
        season = _optional_int(getattr(parsed, "effective_season", None), minimum=0)
        episode = _optional_int(getattr(parsed, "effective_episode", None), minimum=1)
        media_type = _text(getattr(parsed, "media_type", "")).lower()
        if season is not None or episode is not None:
            media_type = "tv"
        if media_type not in _VALID_MEDIA_TYPES:
            continue
        inferred.append({
            "tmdb_id": tmdb_id,
            "title": title,
            "year": _text(getattr(parsed, "year", "")),
            "media_type": media_type,
            "season": season,
            "episode": episode,
        })
    return build_recognition_summary(inferred, source="history_inferred")


def merge_recognition_summaries(
    preferred: Mapping[str, Any] | None,
    fallback: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """用同一媒体的历史推断补齐人工选择，不跨身份合并。"""
    primary = dict(preferred or {})
    secondary = dict(fallback or {})
    if not primary:
        return secondary
    if not secondary:
        return primary
    primary_media = primary.get("media")
    secondary_media = secondary.get("media")
    if not (
        isinstance(primary_media, list) and len(primary_media) == 1
        and isinstance(secondary_media, list) and len(secondary_media) == 1
        and isinstance(primary_media[0], dict) and isinstance(secondary_media[0], dict)
    ):
        return primary
    first = dict(primary_media[0])
    second = secondary_media[0]
    first_id = _text(first.get("tmdb_id"))
    second_id = _text(second.get("tmdb_id"))
    if first_id and second_id and first_id != second_id:
        return primary
    for field in ("tmdb_id", "title", "year", "media_type"):
        if not first.get(field) and second.get(field):
            first[field] = second[field]
    if not first.get("seasons") and second.get("seasons"):
        first["seasons"] = second["seasons"]
    first["file_count"] = max(
        _optional_int(first.get("file_count"), minimum=0) or 0,
        _optional_int(second.get("file_count"), minimum=0) or 0,
    )
    primary["media"] = [first]
    primary["file_count"] = first["file_count"]
    primary["source"] = "manual_selection_with_history"
    return primary
