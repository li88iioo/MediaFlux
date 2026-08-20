"""识别证据的确定性裁决与 fail-closed 安全门。"""
from __future__ import annotations

import re

from app.modules.recognition.cleaner import strip_media_file_suffix



_EXPLICIT_TMDB_WRAPPED_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"(?:\{tmdb-([0-9]{1,10})\}|\(tmdb-([0-9]{1,10})\))"
    r"(?![A-Za-z0-9])"
)

_EXPLICIT_TMDB_BARE_RE = re.compile(
    r"(?i)(?:^|(?<=[\s._()\[\]{}【】]))"
    r"(?:tmdb(?:[ +\-]?)([0-9]{1,10})|tdmb(?:[ +\-]+)([0-9]{1,10}))"
    r"(?=$|(?=[\s._()\[\]{}【】]))"
)



def _explicit_tmdb_marker_ids_from_segment(value: str) -> list[str]:
    """提取单个路径段里的有效显式 TMDB 标记，保留冲突检测信息。"""
    segment = str(value or "").strip()
    # 只剥离真实媒体/伴随文件扩展名；目录名中的版本点号（例如
    # ``Show.2026 {tmdb-123}``）不是扩展名，必须完整保留供标记解析。
    stem = strip_media_file_suffix(segment)
    marker_ids: list[str] = []
    for pattern in (_EXPLICIT_TMDB_WRAPPED_RE, _EXPLICIT_TMDB_BARE_RE):
        for match in pattern.finditer(stem):
            if pattern is _EXPLICIT_TMDB_BARE_RE:
                # 裸标记恰好处于 ``{...}`` / ``(...)`` 内时，必须由上面的
                # wrapped 语法整体通过边界校验。否则 ``word{tmdb-123}word``
                # 会绕过 wrapped 边界，再被内部 bare 子串误识别。
                left = stem[match.start() - 1] if match.start() > 0 else ""
                right = stem[match.end()] if match.end() < len(stem) else ""
                if (left, right) in {("{", "}"), ("(", ")")}:
                    continue
            marker_id = next((group for group in match.groups() if group), "")
            if marker_id and marker_id not in marker_ids:
                marker_ids.append(marker_id)
    return marker_ids

def _strip_explicit_tmdb_markers(value: str) -> str:
    """仅移除语法有效的显式 TMDB 标记，保留其余发布名结构。

    标记常被临时追加在扩展名前。先去掉标记再解析季集，才能让
    ``Title - 153 tmdb123.mkv`` 继续按剧集而不是电影读取 TMDB 详情。
    非法嵌入普通单词的片段保持原样，避免清洗规则比识别规则更宽松。
    """
    text = str(value or "")
    spans: list[tuple[int, int]] = []
    for pattern in (_EXPLICIT_TMDB_WRAPPED_RE, _EXPLICIT_TMDB_BARE_RE):
        for match in pattern.finditer(text):
            if pattern is _EXPLICIT_TMDB_BARE_RE:
                left = text[match.start() - 1] if match.start() > 0 else ""
                right = text[match.end()] if match.end() < len(text) else ""
                if (left, right) in {("{", "}"), ("(", ")")}:
                    continue
            spans.append((match.start(), match.end()))
    if not spans:
        return text
    for start, end in sorted(spans, reverse=True):
        text = f"{text[:start]} {text[end:]}"
    return re.sub(r"\s+", " ", text).strip()

def _strip_explicit_tmdb_markers_from_path(value: str) -> str:
    """逐路径段移除有效 TMDB 标记，同时保留原始路径分隔符。"""
    return "".join(
        part
        if re.fullmatch(r"[/\\]+", part)
        else _strip_explicit_tmdb_markers(part)
        for part in re.split(r"([/\\]+)", str(value or ""))
    )

def _has_explicit_tmdb_marker(value: str) -> bool:
    """判断路径中是否存在至少一个语法有效的显式 TMDB 标记。

    与 ``_explicit_tmdb_id_from_path`` 分开保留此布尔判断，是为了让整理
    目录缓存遇到同级冲突标记时也保持关闭，而不是把冲突误当成“无标记”。
    """
    return any(
        _explicit_tmdb_marker_ids_from_segment(segment)
        for segment in re.split(r"[/\\]+", str(value or ""))
        if str(segment or "").strip()
    )

def _resolve_explicit_tmdb_marker(
    value: str, *, nearest_first: bool = False,
) -> tuple[str, bool]:
    """返回 ``(唯一 ID, 是否存在同级冲突)``，保留 fail-closed 语义。

    空 ID 既可能表示没有标记，也可能表示同一路径段出现了多个不同 ID。
    调用方需要区分这两种状态，避免文件名冲突时又静默继承父目录标记。
    """
    segments = [
        item.strip()
        for item in re.split(r"[/\\]+", str(value or ""))
        if item.strip()
    ]
    if nearest_first:
        segments.reverse()
    for segment in segments:
        marker_ids = _explicit_tmdb_marker_ids_from_segment(segment)
        if len(marker_ids) == 1:
            return marker_ids[0], False
        if len(marker_ids) > 1:
            return "", True
    return "", False

def _explicit_tmdb_id_from_path(value: str, *, nearest_first: bool = False) -> str:
    """从文件名或目录路径提取用户明确标注的 TMDB ID。

    接受 ``{tmdb-123}``、``(tmdb-123)``、``tmdb123``、``tmdb 123``、
    ``tmdb+123``，以及带显式分隔符的常见误拼 ``tdmb+123``。数字仅允许
    ASCII 1-10 位；嵌在普通单词或发布参数中的模糊片段仍会被拒绝。
    """
    marker_id, _conflict = _resolve_explicit_tmdb_marker(
        value, nearest_first=nearest_first,
    )
    return marker_id

def _strict_non_negative_int(value) -> int | None:
    """读取 TMDB 位置字段；拒绝 bool、浮点截断和负数。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
        return int(value.strip())
    return None

def _airing_episode_limit(detail: dict, season: int) -> int | None:
    """连载指针给出的目标季已登记集数上限。

    连载中番剧的 ``seasons[].episode_count`` 常滞后于实际已播集数，而
    ``last_episode_to_air``/``next_episode_to_air`` 是 TMDB 逐集登记的
    权威对象；两者都只在季号精确匹配时采信。
    """
    limit: int | None = None
    for marker in (detail.get("last_episode_to_air"), detail.get("next_episode_to_air")):
        if not isinstance(marker, dict):
            continue
        if _strict_non_negative_int(marker.get("season_number")) != season:
            continue
        number = _strict_non_negative_int(marker.get("episode_number"))
        if number:
            limit = number if limit is None else max(limit, number)
    return limit


def _validate_tmdb_position(
    detail: dict,
    media_type: str,
    season: int | None,
    episode: int | None,
) -> dict[str, object]:
    """验证自动匹配声称的 TV 季/集确实存在于 TMDB 详情。"""
    required = media_type == "tv" and (season is not None or episode is not None)
    result: dict[str, object] = {
        "required": required,
        "passed": True,
        "season": season,
        "episode": episode,
        "reason": "not_required",
    }
    if not required:
        return result

    effective_season = season if season is not None else 1
    result["season"] = effective_season
    seasons = detail.get("seasons") if isinstance(detail, dict) else None
    if not isinstance(seasons, list):
        result.update(passed=False, reason="seasons_missing")
        return result

    matched_season = None
    for item in seasons:
        if not isinstance(item, dict):
            continue
        if _strict_non_negative_int(item.get("season_number")) == effective_season:
            matched_season = item
            break
    if matched_season is None:
        result.update(passed=False, reason="season_not_found")
        return result

    episode_count = _strict_non_negative_int(matched_season.get("episode_count"))
    result["episode_count"] = episode_count
    if episode is None:
        result.update(passed=True, reason="season_verified")
        return result
    airing_limit = _airing_episode_limit(detail, effective_season)
    if episode_count is None:
        if airing_limit is not None and 1 <= episode <= airing_limit:
            result.update(passed=True, reason="episode_verified_airing")
            return result
        result.update(passed=False, reason="episode_count_missing")
        return result
    if episode < 1 or episode > episode_count:
        # 连载指针覆盖到的集号仍是 TMDB 已登记对象；该放行只解锁位置
        # 安全门，reason 与严格的 episode_verified 区分，身份凭证与目标
        # 季年份证据继续要求精确验证。
        if airing_limit is not None and 1 <= episode <= airing_limit:
            result.update(passed=True, reason="episode_verified_airing")
            return result
        result.update(passed=False, reason="episode_out_of_range")
        return result
    result.update(passed=True, reason="episode_verified")
    return result

def _target_episode_air_year(
    season_episodes: object, target_episode: int | None,
) -> str:
    """取出已验证目标集的播出年份；缺少确切日期时返回空串。"""
    if not isinstance(season_episodes, list) or target_episode is None:
        return ""
    for item in season_episodes:
        if not isinstance(item, dict):
            continue
        if _strict_non_negative_int(item.get("episode_number")) != target_episode:
            continue
        air_date = str(item.get("air_date") or "").strip()
        return air_date[:4] if re.fullmatch(r"(?:19|20)\d{2}", air_date[:4]) else ""
    return ""


def _source_year_matches_tmdb(
    detail: dict,
    media_type: str,
    source_year: str,
    *,
    target_season: int | None = None,
    target_episode: int | None = None,
    season_episodes: object = None,
) -> tuple[bool, str]:
    """校验来源年份；TV 可用对应目标季首播年，电影仍只认作品年份。

    跨年季与 split-cour 中，季首播年与具体集的播出年可能不同。传入已验证
    季集映射对应的季集清单时，允许用该集的确切 ``air_date`` 年份补充判断，
    但绝不用模糊年份覆盖已经成立的硬冲突。
    """
    expected = str(source_year or "").strip()
    if not expected:
        return True, "not_provided"
    detail_date = str(
        detail.get("first_air_date") or detail.get("release_date") or ""
    ).strip()
    if detail_date[:4] == expected:
        return True, "series_or_movie_year"
    if media_type != "tv" or target_season is None:
        return False, "year_mismatch"
    seasons = detail.get("seasons") if isinstance(detail, dict) else None
    if not isinstance(seasons, list):
        return False, "season_year_unavailable"
    for item in seasons:
        if not isinstance(item, dict):
            continue
        if _strict_non_negative_int(item.get("season_number")) != target_season:
            continue
        season_year = str(item.get("air_date") or "").strip()[:4]
        if season_year == expected:
            return True, "target_season_year"
        if _target_episode_air_year(season_episodes, target_episode) == expected:
            return True, "target_episode_air_year"
        return False, "target_season_year_mismatch"
    return False, "target_season_not_found"

def _tmdb_position_error(validation: dict[str, object]) -> str:
    """把季集校验诊断转换为稳定、可直接展示的人工确认原因。"""
    reason = str(validation.get("reason") or "position_unverified")
    return {
        "seasons_missing": "TMDB 详情未提供季信息，无法确认文件季集，需人工确认",
        "season_not_found": "文件季号在 TMDB 中不存在，已阻止自动整理",
        "episode_count_missing": "TMDB 未提供该季集数，无法确认文件集号，需人工确认",
        "episode_out_of_range": "文件集号超出 TMDB 记录范围，已阻止自动整理",
    }.get(reason, "TMDB 未能确认文件季集位置，需人工确认")
