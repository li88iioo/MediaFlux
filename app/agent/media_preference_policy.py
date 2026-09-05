"""结构化媒体偏好的校验与消费策略；不解释自然语言、不执行任何写操作。"""
from __future__ import annotations

import math
import re
import unicodedata
from typing import Any

from app.agent.action_history import action_history_owner_digest
from app.agent.errors import AgentToolError
from app.repositories.media_experience import (
    default_media_preferences,
    get_media_preferences,
)

_ENUMS = {
    "preferred_server": ("any", "jellyfin", "emby"),
    "preferred_download_target": ("qb", "guangya", "both"),
    "preferred_resolution": ("any", "720p", "1080p", "2160p"),
    "minimum_resolution": ("any", "720p", "1080p", "2160p"),
    "preferred_hdr": ("any", "sdr", "hdr10", "hdr10+", "dolby_vision"),
}
_LIST_LIMITS = {
    "preferred_codecs": 3,
    "preferred_subtitles": 12,
    "preferred_audio_languages": 12,
    "preferred_release_groups": 12,
    "excluded_keywords": 24,
    "preferred_genres": 12,
    "excluded_genres": 8,
}
_CODECS = ("h264", "hevc", "av1")
_RESOLUTIONS = {"any": 0, "720p": 720, "1080p": 1080, "2160p": 2160}
PREFERENCE_FIELDS = tuple(default_media_preferences())
PREFERENCE_PROPERTIES: dict[str, Any] = {
    **{key: {"type": "string", "enum": list(values)} for key, values in _ENUMS.items()},
    **{
        key: {
            "type": "array", "maxItems": maximum, "uniqueItems": True,
            "items": ({"type": "string", "enum": list(_CODECS)} if key == "preferred_codecs"
                      else {"type": "string", "minLength": 1, "maxLength": 80}),
        }
        for key, maximum in _LIST_LIMITS.items()
    },
    "max_episode_size_gb": {"type": "number", "minimum": 0, "maximum": 200,
                            "description": "单集最大 GiB，0 表示不限；整季包不据总大小误判。"},
    "min_rating": {"type": "number", "minimum": 0, "maximum": 10},
    "exclude_played": {"type": "boolean"},
}

RESOURCE_PREFERENCE_FIELDS = (
    "preferred_resolution", "minimum_resolution", "preferred_hdr", "preferred_codecs",
    "preferred_subtitles", "preferred_audio_languages", "preferred_release_groups",
    "excluded_keywords", "max_episode_size_gb",
)
RESOURCE_PREFERENCE_PROPERTIES = {
    key: PREFERENCE_PROPERTIES[key] for key in RESOURCE_PREFERENCE_FIELDS
}


def validate_resource_preference_overrides(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - set(RESOURCE_PREFERENCE_FIELDS):
        raise AgentToolError("preference_overrides 只接受资源质量偏好字段")
    return validate_preference_updates(value) if value else {}


def validate_preference_updates(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or not arguments or set(arguments) - set(PREFERENCE_FIELDS):
        raise AgentToolError("媒体偏好字段无效或为空")
    result: dict[str, Any] = {}
    for key, raw in arguments.items():
        if key in _ENUMS:
            value = str(raw or "").strip().casefold()
            if value not in _ENUMS[key]:
                raise AgentToolError(f"{key} 的取值不受支持")
        elif key in _LIST_LIMITS:
            if not isinstance(raw, list) or len(raw) > _LIST_LIMITS[key]:
                raise AgentToolError(f"{key} 必须是最多 {_LIST_LIMITS[key]} 项的数组")
            value = []
            seen: set[str] = set()
            for item in raw:
                if not isinstance(item, str):
                    raise AgentToolError(f"{key} 中的匹配词必须是字符串")
                term = unicodedata.normalize("NFKC", item).strip()
                if (not term or len(term) > 80 or
                        any(unicodedata.category(char).startswith("C") for char in term)):
                    raise AgentToolError(f"{key} 中包含无效匹配词")
                if key in {"preferred_genres", "excluded_genres"} and any(
                    not part.strip() for part in term.split("|")
                ):
                    raise AgentToolError(f"{key} 中包含空的同义匹配词")
                if key == "preferred_codecs":
                    term = term.casefold()
                    if term not in _CODECS:
                        raise AgentToolError("preferred_codecs 仅支持 h264、hevc、av1")
                if term.casefold() not in seen:
                    value.append(term)
                    seen.add(term.casefold())
        elif key == "exclude_played":
            if not isinstance(raw, bool):
                raise AgentToolError("exclude_played 必须是布尔值")
            value = raw
        else:
            maximum = 200 if key == "max_episode_size_gb" else 10
            if (isinstance(raw, bool) or not isinstance(raw, (int, float)) or
                    not 0 <= raw <= maximum or not math.isfinite(raw)):
                raise AgentToolError(f"{key} 必须是 0 到 {maximum} 的有限数字")
            value = float(raw)
        result[key] = value
    return result


def effective_preferences(
    preferences: dict[str, Any], overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """字段级单次覆盖，包括显式空数组/0/False，不能被长期偏好覆盖。"""
    result = default_media_preferences()
    for key in PREFERENCE_FIELDS:
        if key in preferences:
            try:
                result.update(validate_preference_updates({key: preferences[key]}))
            except AgentToolError:
                pass  # 损坏的存量字段不应中断只读推荐。
    if overrides:
        result.update(validate_preference_updates(overrides))
    return result


def owner_media_preferences(owner: str) -> dict[str, Any]:
    owner = str(owner or "").strip()
    if not owner:
        return default_media_preferences()
    return effective_preferences(get_media_preferences(action_history_owner_digest(owner)))


def _contains(text: str, term: str) -> bool:
    """普通词匹配；偏好文本永不作为正则执行。"""
    normalized = unicodedata.normalize("NFKC", term).casefold()
    return any(part.strip() and part.strip() in text for part in normalized.split("|"))


_LANGUAGE_MARKERS = {
    "zh-hans": ("简体", "简中", "chs", "简繁", "繁简"),
    "简体中文": ("简体", "简中", "chs", "简繁", "繁简"),
    "简中": ("简体", "简中", "chs", "简繁", "繁简"),
    "zh-hant": ("繁体", "繁中", "cht", "简繁", "繁简"),
    "繁体中文": ("繁体", "繁中", "cht", "简繁", "繁简"),
    "en": ("英文", "英语", "英字", "eng", "english", "简繁英"),
    "英语": ("英文", "英语", "英字", "eng", "english", "简繁英"),
    "ja": ("日语", "日音", "jpn", "japanese"),
    "日语": ("日语", "日音", "jpn", "japanese"),
    "zh": ("国语", "国配", "中文音轨", "mandarin"),
    "国语": ("国语", "国配", "中文音轨", "mandarin"),
}


def _language_match(title: str, term: str) -> bool:
    aliases = _LANGUAGE_MARKERS.get(term.casefold(), (term,))
    return any(_contains(title, marker) for marker in aliases)


def resource_preference_match(
    item: dict[str, Any], preferences: dict[str, Any], *, single_episode: bool = False,
) -> dict[str, Any]:
    """只基于资源站返回的文件标签排序；未知标签不冒充已匹配。"""
    profile = effective_preferences(preferences)
    title = unicodedata.normalize("NFKC", str(item.get("title") or "")).casefold()
    reasons: list[str] = []
    warnings: list[str] = []
    score = 0
    eligible = True
    for term in profile["excluded_keywords"]:
        if _contains(title, term):
            warnings.append(f"命中排除词：{term}")
            eligible = False
    resolution = next((level for level, pattern in (
        (2160, r"(?<![a-z0-9])(?:2160p?|4k)(?![a-z0-9])"),
        (1080, r"(?<![a-z0-9])1080[pi]?(?![a-z0-9])"),
        (720, r"(?<![a-z0-9])720p?(?![a-z0-9])"),
    ) if re.search(pattern, title)), 0)
    minimum = _RESOLUTIONS[profile["minimum_resolution"]]
    if minimum and resolution and resolution < minimum:
        eligible = False
        warnings.append("低于偏好中的最低分辨率")
    elif minimum and not resolution:
        eligible = False
        warnings.append("分辨率未知，无法核对最低要求")
    if resolution and resolution == _RESOLUTIONS[profile["preferred_resolution"]]:
        score += 45
        reasons.append(f"偏好分辨率：{profile['preferred_resolution']}")
    dolby = bool(re.search(r"(?<![a-z0-9])(?:dv|dovi)(?![a-z0-9])|dolby[ ._-]*vision|杜比视界", title))
    hdr = "dolby_vision" if dolby else "hdr10+" if "hdr10+" in title else "hdr10" if "hdr" in title else "sdr" if "sdr" in title else ""
    if profile["preferred_hdr"] != "any" and hdr == profile["preferred_hdr"]:
        score += 25
        reasons.append(f"偏好画面格式：{hdr}")
    codec_patterns = {"hevc": r"hevc|h[ ._-]?265|x265", "h264": r"h[ ._-]?264|x264|avc", "av1": r"(?<![a-z0-9])av1(?![a-z0-9])"}
    for index, codec in enumerate(profile["preferred_codecs"]):
        if re.search(codec_patterns[codec], title):
            score += 18 - index * 3
            reasons.append(f"偏好编码：{codec}")
            break
    for key, label, weight in (
        ("preferred_subtitles", "字幕", 15),
        ("preferred_audio_languages", "音轨", 12),
        ("preferred_release_groups", "发布组", 20),
    ):
        for index, term in enumerate(profile[key]):
            matches = _contains(title, term) if key == "preferred_release_groups" else _language_match(title, term)
            if matches:
                score += max(3, weight - index)
                reasons.append(f"偏好{label}：{term}")
                break
    maximum = profile["max_episode_size_gb"]
    if maximum and single_episode:
        size = item.get("size_bytes")
        if isinstance(size, (int, float)) and not isinstance(size, bool) and size > 0 and (isinstance(size, int) or math.isfinite(size)):
            if size > maximum * (1024 ** 3):
                eligible = False
                warnings.append("超过偏好中的单集大小限制")
        else:
            eligible = False
            warnings.append("文件大小未知，无法核对单集大小限制")
    return {"score": score, "eligible": eligible, "reasons": reasons, "warnings": warnings}
