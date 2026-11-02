"""跨入口共享的媒体身份键与季集标量规范化。"""
from __future__ import annotations

import re
from typing import Any

_TMDB_ID_RE = re.compile(r"[0-9]{1,10}")
_EPISODE_LABEL_RE = re.compile(r"(?i)^(?:S(?P<season>[0-9]{1,3}))?E(?P<episode>[0-9]{1,4})$")


def normalize_tmdb_id(value: object) -> str:
    """规范数字 TMDB ID；拒绝模糊值和零。"""
    raw = str(value or "").strip()
    if not _TMDB_ID_RE.fullmatch(raw):
        raise ValueError("TMDB ID 必须是 1 至 10 位数字")
    normalized = str(int(raw))
    if normalized == "0":
        raise ValueError("TMDB ID 不能为 0")
    return normalized


def build_media_key(
    tmdb_id: object,
    media_type: str,
    season: int | None = None,
    episode: int | None = None,
) -> str:
    """构造可供 RSS 与媒体订阅共同使用的 canonical key。"""
    normalized_id = normalize_tmdb_id(tmdb_id)
    normalized_type = (media_type or "").strip().lower()
    if normalized_type == "movie":
        return f"tmdb:{normalized_id}:movie"
    if normalized_type != "tv" or season is None or episode is None:
        raise ValueError("剧集媒体键缺少季集号")
    normalized_season = season
    normalized_episode = episode
    if not 0 <= normalized_season <= 100 or not 1 <= normalized_episode <= 9999:
        raise ValueError("剧集季集号超出支持范围")
    return f"tmdb:{normalized_id}:tv:S{normalized_season:02d}E{normalized_episode:03d}"


def parse_episode_label(value: object, *, default_season: int = 1) -> tuple[int, int] | None:
    """只解析结构化 SxxEyy / Exx 标签，不以标题相似度猜测媒体身份。"""
    raw = str(value or "").strip()
    match = _EPISODE_LABEL_RE.fullmatch(raw)
    if not match:
        return None
    season = int(match.group("season")) if match.group("season") is not None else default_season
    episode = int(match.group("episode"))
    if not 0 <= season <= 100 or not 1 <= episode <= 9999:
        return None
    return season, episode


def normalize_media_number(value: Any) -> int | None:
    """把外部解析器的季集值收敛为 SQLite 可绑定标量。"""
    candidates = value if isinstance(value, (list, tuple, set)) else (value,)
    for candidate in candidates:
        if candidate in (None, "") or isinstance(candidate, bool):
            continue
        try:
            number = int(float(candidate))
        except (TypeError, ValueError, OverflowError):
            continue
        if number >= 0:
            return number
    return None


def confirmed_manual_position(release_parse: object) -> dict | None:
    """读取已确认的季集覆盖；预览与持久学习使用同一份格式约束。"""
    if not isinstance(release_parse, dict):
        return None
    position = release_parse.get("manual_position")
    if not isinstance(position, dict) or position.get("source") != "manual_correction" or type(position.get("version")) is not int or position["version"] != 1:
        return None
    season, episode = position.get("season"), position.get("episode")
    if type(season) is not int or not 0 <= season <= 999:
        return None
    if episode is not None and (type(episode) is not int or not 1 <= episode <= 999):
        return None
    return dict(position)


def tmdb_alias_values(candidate: dict) -> list[object]:
    """共享 TMDB 别名/翻译遍历；调用方保留各自的去重和标题比较语义。"""
    values: list[object] = []
    for key in ("aliases", "alternative_titles", "translations"):
        collection = candidate.get(key) or []
        if isinstance(collection, dict):
            collection = (
                collection.get("titles")
                or collection.get("results")
                or collection.get("translations")
                or []
            )
        for item in collection if isinstance(collection, list) else []:
            if isinstance(item, dict):
                data = item.get("data") if isinstance(item.get("data"), dict) else {}
                values.extend((
                    item.get("title"), item.get("name"),
                    data.get("title"), data.get("name"), data.get("english_name"),
                ))
            else:
                values.append(item)
    return values
