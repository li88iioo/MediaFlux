"""人工确认产生的轻量媒体别名与负反馈。

该模块只学习用户明确确认过的标题映射，不从低置信度自动结果自学习。
别名命中仍会回读 TMDB 详情，因此不会绕过媒体类型和详情有效性校验。
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

from app.database import get_conn, now
from app.logger import get_logger

logger = get_logger(__name__)

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
    rejected_tmdb_ids: Iterable[object] = (),
) -> int:
    """记录人工确认映射，并阻止同年份的旧冲突映射再次命中。"""
    normalized_type = "tv" if str(media_type or "").strip().lower() == "tv" else "movie"
    selected_id = str(tmdb_id or "").strip()
    if not selected_id.isdigit():
        return 0
    cleaned = _clean_aliases(aliases)
    if not cleaned:
        return 0
    selected_year = str(year or "").strip()[:4]
    timestamp = now()
    rejected = {
        str(value or "").strip()
        for value in rejected_tmdb_ids
        if str(value or "").strip().isdigit() and str(value or "").strip() != selected_id
    }
    with get_conn() as conn:
        for alias, normalized in cleaned:
            # 用户对同一标题/年份重新选择时，旧结论成为明确负反馈；不同年份
            # 的重拍版允许并存，查询阶段再用年份消歧。
            if selected_year:
                conn.execute(
                    "UPDATE media_title_aliases SET blocked=1,updated_at=? "
                    "WHERE normalized_alias=? AND media_type=? AND tmdb_id<>? "
                    "AND year=?",
                    (timestamp, normalized, normalized_type, selected_id, selected_year),
                )
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
            for rejected_id in rejected:
                conn.execute(
                    "INSERT INTO media_title_aliases("
                    "normalized_alias,alias,tmdb_id,title,year,media_type,source,blocked,"
                    "hit_count,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(normalized_alias,media_type,tmdb_id) DO UPDATE SET "
                    "blocked=1,source='manual_rejected',updated_at=excluded.updated_at",
                    (
                        normalized, alias, rejected_id, "", selected_year,
                        normalized_type, "manual_rejected", 1, 0,
                        timestamp, timestamp,
                    ),
                )
    return len(cleaned)


def lookup_manual_alias(
    aliases: Iterable[object],
    *,
    media_type: object,
    year: object = "",
) -> dict[str, Any] | None:
    """仅在全部有效别名证据指向唯一 TMDB 条目时返回命中。"""
    normalized_type = "tv" if str(media_type or "").strip().lower() == "tv" else "movie"
    cleaned = _clean_aliases(aliases)
    if not cleaned:
        return None
    normalized_values = [item[1] for item in cleaned]
    placeholders = ",".join("?" for _ in normalized_values)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM media_title_aliases WHERE media_type=? AND blocked=0 "
            f"AND normalized_alias IN ({placeholders}) ORDER BY updated_at DESC,id DESC",
            (normalized_type, *normalized_values),
        ).fetchall()
    expected_year = str(year or "").strip()[:4]
    valid: list[Any] = []
    for row in rows:
        row_year = str(row["year"] or "")[:4]
        # 带年份的人工映射只在请求也具备年份时参与自动锁定。缺少年份的
        # 新文件可能是同名重拍版，不能因为数据库中仅有一个旧映射就短路
        # 正常的 TMDB 候选识别；明确以空年份记录的别名仍可复用。
        if not expected_year and row_year:
            continue
        if expected_year and row_year and expected_year != row_year:
            # 人工确认属于强锁。年份相差 1 也可能是同名重拍或跨年播出，
            # 不应像普通搜索候选那样宽松复用；缺年份映射仍可由标题证据使用。
            continue
        valid.append(row)
    ids = {str(row["tmdb_id"] or "") for row in valid if str(row["tmdb_id"] or "")}
    if len(ids) != 1:
        return None
    selected_id = ids.pop()
    selected = next(row for row in valid if str(row["tmdb_id"] or "") == selected_id)
    try:
        with get_conn() as conn:
            conn.execute(
                "UPDATE media_title_aliases SET hit_count=hit_count+1,updated_at=? "
                "WHERE id=?",
                (now(), int(selected["id"])),
            )
    except Exception as exc:
        logger.debug("媒体别名命中计数写入失败 type=%s", type(exc).__name__)
    return dict(selected)
