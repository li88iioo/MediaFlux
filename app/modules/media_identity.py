"""跨入口共享的可信媒体身份键。"""
from __future__ import annotations

import re

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
    normalized_type = str(media_type or "").strip().lower()
    if normalized_type == "movie":
        return f"tmdb:{normalized_id}:movie"
    if normalized_type != "tv" or season is None or episode is None:
        raise ValueError("剧集媒体键缺少季集号")
    normalized_season = int(season)
    normalized_episode = int(episode)
    if not 0 <= normalized_season <= 100 or not 1 <= normalized_episode <= 9999:
        raise ValueError("剧集季集号超出支持范围")
    return f"tmdb:{normalized_id}:tv:S{normalized_season:02d}E{normalized_episode:03d}"


def parse_episode_label(value: object, *, default_season: int = 1) -> tuple[int, int] | None:
    """只解析结构化 SxxEyy / Exx 标签，不以标题相似度猜测媒体身份。"""
    raw = str(value or "").strip()
    match = _EPISODE_LABEL_RE.fullmatch(raw)
    if not match:
        return None
    season = int(match.group("season")) if match.group("season") is not None else int(default_season)
    episode = int(match.group("episode"))
    if not 0 <= season <= 100 or not 1 <= episode <= 9999:
        return None
    return season, episode
