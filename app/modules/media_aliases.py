"""人工确认产生的候选召回线索；不得替代作品身份与季集校验。"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

from app.database import get_conn, now

_MAX_ALIAS_LENGTH = 240
_MAX_ALIASES_PER_CONFIRMATION = 16
_GENERIC_ALIASES = {
    "anime", "episode", "film", "movie", "special", "tv", "unknown", "video",
    "动漫", "動畫", "动画", "剧集", "劇集", "电影", "電影", "影片", "未知",
}


def normalize_alias(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(
        r"[^a-z0-9\u3040-\u30ff\u31f0-\u31ff\u3400-\u9fff"
        r"\uac00-\ud7af\u1100-\u11ff\u3130-\u318f]+",
        " ",
        text.casefold(),
    ).strip()


def _clean_aliases(values: Iterable[object]) -> list[tuple[str, str]]:
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for value in values:
        alias = re.sub(r"\s+", " ", str(value or "")).strip(" ._-·")
        normalized = normalize_alias(alias)
        compact = normalized.replace(" ", "")
        if (
            not alias
            or len(alias) > _MAX_ALIAS_LENGTH
            or len(compact) < 4
            or compact.isdigit()
            or normalized in _GENERIC_ALIASES
            or normalized in seen
        ):
            continue
        seen.add(normalized)
        result.append((alias, normalized))
        if len(result) >= _MAX_ALIASES_PER_CONFIRMATION:
            break
    return result


def record_manual_confirmation(
    aliases: Iterable[object],
    *,
    tmdb_id: object,
    title: object = "",
    year: object = "",
    media_type: object,
) -> int:
    """保留确认过的候选，不把单文件选择推广为其他作品的全局负反馈。"""
    normalized_type = "tv" if str(media_type or "").strip().lower() == "tv" else "movie"
    selected_id = str(tmdb_id or "").strip()
    if not selected_id.isdigit():
        return 0
    cleaned = _clean_aliases(aliases)
    if not cleaned:
        return 0
    selected_year = str(year or "").strip()[:4]
    timestamp = now()
    with get_conn() as conn:
        for alias, normalized in cleaned:
            conn.execute(
                "INSERT INTO media_title_aliases("
                "normalized_alias,alias,tmdb_id,title,year,media_type,source,blocked,"
                "hit_count,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(normalized_alias,media_type,tmdb_id) DO UPDATE SET "
                "alias=excluded.alias,title=excluded.title,year=excluded.year,"
                "source='manual',blocked=0,updated_at=excluded.updated_at",
                (
                    normalized, alias, selected_id, str(title or "").strip(),
                    selected_year, normalized_type, "manual", 0, 0,
                    timestamp, timestamp,
                ),
            )
    return len(cleaned)


def lookup_manual_aliases(
    aliases: Iterable[object], *, media_type: object,
) -> list[dict[str, Any]]:
    """仅召回候选ID；年份缺省、重拍和冲突交由统一候选评分处理。"""
    normalized_type = "tv" if str(media_type or "").strip().lower() == "tv" else "movie"
    cleaned = _clean_aliases(aliases)
    if not cleaned:
        return []
    normalized_values = [item[1] for item in cleaned]
    placeholders = ",".join("?" for _ in normalized_values)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM media_title_aliases WHERE media_type=? AND blocked=0 "
            f"AND normalized_alias IN ({placeholders}) ORDER BY updated_at DESC,id DESC",
            (normalized_type, *normalized_values),
        ).fetchall()
    candidates: dict[str, dict[str, Any]] = {}
    for row in rows:
        candidates.setdefault(str(row["tmdb_id"]), dict(row))
    return list(candidates.values())
