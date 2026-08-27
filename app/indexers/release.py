"""Indexer-specific release title parsing helpers.

The library filename parser deliberately stays conservative. Indexer titles are noisier and
frequently split season and episode markers across different title segments (for example
``[第29-30集] ... S02``), so the search/ranking layer needs a slightly broader parser.
"""

from __future__ import annotations

import re
import unicodedata

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
_EPISODE = re.compile(r"(?ix)(?<![a-z0-9])(?:ep?|episode)[ ._\-]*0*(\d{1,4})(?!\d)")
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
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None
    return None


def parse_indexer_release_position(value: str) -> dict[str, int | None]:
    """Extract season/episode/range from an indexer release title.

    Compared with :func:`app.modules.scraper.parse_release_position`, this also binds a season
    marker found later in the title to Chinese episode markers found earlier in the title.
    """

    text = unicodedata.normalize("NFKC", str(value or ""))
    base = parse_release_position(text)
    season = _valid_season(base.get("season"))
    episode = _valid_episode(base.get("episode"))
    episode_end = _valid_episode(base.get("episode_end"))

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

    return {
        "season": _valid_season(season),
        "episode": _valid_episode(episode),
        "episode_end": _valid_episode(episode_end),
    }


def release_covers_target(
    value: str,
    *,
    season: int | None,
    episode: int | None,
) -> tuple[str, dict[str, int | None]]:
    """Classify how a release position relates to the requested season/episode."""

    position = parse_indexer_release_position(value)
    parsed_season = position["season"]
    parsed_episode = position["episode"]
    parsed_end = position["episode_end"] or parsed_episode

    if season is not None and parsed_season is not None and parsed_season != season:
        return "conflict", position
    if episode is None:
        if season is not None and parsed_season == season:
            return "season", position
        return "unknown", position
    if parsed_episode is None:
        if season is not None and parsed_season == season:
            return "season", position
        return "unknown", position
    if parsed_episode <= episode <= (parsed_end or parsed_episode):
        if parsed_episode == episode and parsed_end == episode:
            return "exact", position
        return "range", position
    return "conflict", position
