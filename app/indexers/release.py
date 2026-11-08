"""Indexer-specific release title parsing helpers.

The library filename parser deliberately stays conservative. Indexer titles are noisier and
frequently split season and episode markers across different title segments (for example
``[第29-30集] ... S02``), so the search/ranking layer needs a slightly broader parser.
"""

from __future__ import annotations

import re
import unicodedata

from app.modules.episode_mapping import extract_release_episode_range
from app.modules.scraper import parse_release_position

_SEASON = re.compile(
    r"(?ix)(?:"
    r"(?<![a-z0-9])s(?:eason)?[ ._\-]*0*(\d{1,3})(?!\d)"
    r"|第\s*0*(\d{1,3})\s*季"
    r")"
)
_SEASON_EPISODE_RANGE = re.compile(
    r"(?ix)(?<![a-z0-9])s[ ._\-]*0*(\d{1,3})[ ._\-]*e[ ._\-]*0*(\d{1,4})"
    r"\s*(?:-|~|～|to|至)\s*(?:s[ ._\-]*0*\d{1,3}[ ._\-]*)?e?[ ._\-]*0*(\d{1,4})(?!\d)"
)
_SEASON_EPISODE = re.compile(
    r"(?ix)(?<![a-z0-9])s[ ._\-]*0*(\d{1,3})[ ._\-]*e[ ._\-]*0*(\d{1,4})(?!\d)"
)
_X_EPISODE_RANGE = re.compile(
    r"(?ix)(?<!\d)0*(\d{1,3})\s*x\s*0*(\d{1,4})"
    r"\s*(?:-|~|～|to|至)\s*0*(\d{1,4})(?!\d)"
)
_CHINESE_EPISODE_RANGE = re.compile(
    r"第\s*0*(\d{1,4})\s*(?:-|~|～|至)\s*(?:第\s*)?0*(\d{1,4})\s*[集話话]",
    re.IGNORECASE,
)
_EPISODE_RANGE = re.compile(
    r"(?ix)(?<![a-z0-9])(?:ep?|episode)[ ._\-]*0*(\d{1,4})"
    r"\s*(?:-|~|～|to|至)\s*(?:ep?|episode)?[ ._\-]*0*(\d{1,4})(?!\d)"
)
_CHINESE_EPISODE = re.compile(r"第\s*0*(\d{1,4})\s*[集話话]", re.IGNORECASE)
_EPISODE = re.compile(
    r"(?ix)(?<![a-z0-9])(?:ep?|episode)[ ._\-]*0*(\d{1,4})(?:v\d+)?(?![a-z0-9])"
)
_COMPLETE_PACK = re.compile(r"(?:全集|全)\s*0*(\d{1,4})\s*[集話话]", re.IGNORECASE)


def _valid_season(value: int | None) -> int | None:
    return value if value is not None and 0 <= value <= 100 else None


def _valid_episode(value: int | None) -> int | None:
    return value if value is not None and 1 <= value <= 1000 else None


def _first_int(match: re.Match[str] | None) -> int | None:
    if match is None:
        return None
    for raw in match.groups():
        if raw is not None:
            return int(raw)
    return None


def parse_indexer_release_position(value: str, *, media_title: str = "") -> dict[str, int | None]:
    """Extract season/episode/range from an indexer release title.

    Compared with :func:`app.modules.scraper.parse_release_position`, this also binds a season
    marker found later in the title to Chinese episode markers found earlier in the title.
    """

    text = unicodedata.normalize("NFKC", (value or ""))
    base = parse_release_position(text, infer_unnumbered_special=False)
    season = _valid_season(base.get("season"))
    episode = _valid_episode(base.get("episode"))
    episode_end = _valid_episode(base.get("episode_end"))
    # 特别篇/小数集是一组位置事实，不能用另一处普通季号替换其中一半。
    if season == 0:
        return {"season": season, "episode": episode, "episode_end": episode_end}

    match = _SEASON_EPISODE_RANGE.search(text)
    if match is not None:
        parsed_season, start, end = (int(raw) for raw in match.groups())
        if _valid_season(parsed_season) is not None and 1 <= start <= end <= 1000:
            return {"season": parsed_season, "episode": start, "episode_end": end}

    match = _X_EPISODE_RANGE.search(text)
    if match is not None:
        parsed_season, start, end = (int(raw) for raw in match.groups())
        if _valid_season(parsed_season) is not None and 1 <= start <= end <= 1000:
            return {"season": parsed_season, "episode": start, "episode_end": end}

    direct = _SEASON_EPISODE.search(text)
    if direct is not None:
        parsed_season, parsed_episode = (int(raw) for raw in direct.groups())
        if _valid_season(parsed_season) is not None and _valid_episode(parsed_episode) is not None:
            season, episode = parsed_season, parsed_episode

    explicit_season = _first_int(_SEASON.search(text))
    if _valid_season(explicit_season) is not None:
        season = explicit_season

    range_match = _CHINESE_EPISODE_RANGE.search(text) or _EPISODE_RANGE.search(text)
    if range_match is not None:
        start, end = (int(raw) for raw in range_match.groups())
        start, end = sorted((start, end))
        if 1 <= start <= end <= 1000:
            episode, episode_end = start, end
    else:
        single_match = _CHINESE_EPISODE.search(text) or _EPISODE.search(text)
        parsed_episode = _first_int(single_match)
        if _valid_episode(parsed_episode) is not None:
            episode, episode_end = parsed_episode, None
        else:
            complete_match = _COMPLETE_PACK.search(text)
            complete_end = _first_int(complete_match)
            if _valid_episode(complete_end) is not None:
                episode, episode_end = 1, complete_end

    if episode is None:
        range_season, start, end = extract_release_episode_range(text)
        if _valid_episode(start) is not None and _valid_episode(end) is not None:
            season = season if season is not None else _valid_season(range_season)
            episode, episode_end = start, end

    # 站点可在作品名后直接接裸集号，再接清晰度或分集标题。
    # 只有已核对作品名恰好位于编号前，才采用此格式；
    # 不把作品名自身的数字、年份或清晰度猜成集号，也不补造季号。
    known_title = unicodedata.normalize("NFKC", (media_title or "")).strip()
    technical = re.search(
        rf"(?:^|[\s\]】]){re.escape(known_title)}[ ._\-]*"
        r"(0*[1-9]\d{0,2}(?:\.\d{1,2})?)(?:\s*[-~～至]\s*(0*[1-9]\d{0,2}))?"
        r"[ ._\[【]+(?:480|576|720|1080|1440|2160|4320)p\b",
        text, re.IGNORECASE,
    ) if known_title else None
    if technical and (season is None or episode is None):
        start, end = technical.groups()
        if "." in start:
            return {"season": 0, "episode": None, "episode_end": None}
        if episode is None and (end is None or int(start) <= int(end)):
            episode, episode_end = int(start), int(end) if end else None
    if episode is None and known_title:
        captioned = re.search(
            rf"(?:^|[\s\]】]){re.escape(known_title)}\s+(0*[1-9]\d{{0,2}})(?!\d)\s+(?=[\u3400-\u9fff])",
            text,
            re.IGNORECASE,
        )
        if captioned:
            episode = _valid_episode(int(captioned.group(1)))

    return {
        "season": _valid_season(season),
        "episode": _valid_episode(episode),
        "episode_end": _valid_episode(episode_end),
    }
