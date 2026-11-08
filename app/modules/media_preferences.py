"""结构化媒体偏好的校验、变更预检与条件提交；只保存用户显式变更。"""
from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import unicodedata
from typing import Any

from app.database import media_owner_digest
from app.modules.media_probe import infer_dynamic_ranges
from app.repositories import media_experience as repository
from app.repositories.media_experience import default_media_preferences, get_media_preferences

_PREFERENCE_LABELS = {
    "preferred_server": "首选媒体服务器", "preferred_download_target": "默认下载目标",
    "preferred_resolution": "首选分辨率", "minimum_resolution": "最低分辨率",
    "preferred_hdr": "首选动态范围", "preferred_codecs": "首选编码",
    "preferred_subtitles": "首选字幕", "preferred_audio_languages": "首选音轨语言",
    "preferred_release_groups": "首选发布组", "excluded_keywords": "排除关键词",
    "preferred_genres": "偏好题材", "excluded_genres": "排除题材",
    "max_episode_size_gb": "单集最大大小（GiB，0为不限）", "min_rating": "最低评分",
    "exclude_played": "排除已看内容", "memory_notes": "长期备忘",
}


_PREFERENCE_VALUES = {
    "any": "不限", "dolby_vision": "杜比视界", "hdr10": "HDR10", "hdr10+": "HDR10+", "sdr": "SDR",
    "guangya": "光鸭", "qb": "qBittorrent", "both": "两者", "jellyfin": "Jellyfin", "emby": "Emby",
}


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
    "memory_notes": 20,
}
_TERM_CHARS = {"memory_notes": 200}
_CODECS = ("h264", "hevc", "av1")
_RESOLUTIONS = {"any": 0, "720p": 720, "1080p": 1080, "2160p": 2160}
PREFERENCE_FIELDS = tuple(default_media_preferences())
PREFERENCE_PROPERTIES: dict[str, Any] = {
    **{key: {"type": "string", "enum": list(values)} for key, values in _ENUMS.items()},
    **{
        key: {
            "type": "array", "maxItems": maximum, "uniqueItems": True,
            "items": ({"type": "string", "enum": list(_CODECS)} if key == "preferred_codecs"
                      else {"type": "string", "minLength": 1, "maxLength": _TERM_CHARS.get(key, 80)}),
        }
        for key, maximum in _LIST_LIMITS.items()
    },
    "max_episode_size_gb": {"type": "number", "minimum": 0, "maximum": 200,
                            "description": "单集最大 GiB，0 表示不限；整季包不据总大小误判。"},
    "min_rating": {"type": "number", "minimum": 0, "maximum": 10},
    "exclude_played": {"type": "boolean"},
}
PREFERENCE_PROPERTIES["memory_notes"]["description"] = (
    "长期备忘：结构化字段表达不了、用户明确要求记住的事实或习惯（如“动漫库叫 Anime”“不要推荐韩剧”）。"
    "提交完整新列表：保留仍有效的旧条目，删除用户要求忘记的条目；每条一句，不存密钥、链接或一次性任务。"
)
PREFERENCE_PROPERTIES["preferred_resolution"]["description"] = (
    "软偏好/排序优先级。例如‘默认优先4K’只设置本字段为2160p，不同时设置最低分辨率。"
)
PREFERENCE_PROPERTIES["minimum_resolution"]["description"] = (
    "硬性最低画质过滤；仅用户明确要求至少/不得低于某分辨率时设置。‘优先4K’不代表排除1080p，不应改动此字段。"
)

RESOURCE_PREFERENCE_FIELDS = (
    "preferred_resolution", "minimum_resolution", "preferred_hdr", "preferred_codecs",
    "preferred_subtitles", "preferred_audio_languages", "preferred_release_groups",
    "excluded_keywords", "max_episode_size_gb",
)
RESOURCE_PREFERENCE_PROPERTIES = {
    key: PREFERENCE_PROPERTIES[key] for key in RESOURCE_PREFERENCE_FIELDS
}


def resolve_download_target(owner: str) -> tuple[str, str]:
    """解析默认下载目标与来源；写操作仅在预检调用，确认使用冻结的实际目标。"""
    if normalized_owner := (owner or "").strip():
        preferences = repository.get_media_preferences(media_owner_digest(normalized_owner))
        target = str(preferences.get("preferred_download_target") or "").strip()
        if preferences.get("explicit") and target in {"qb", "guangya", "both"}:
            return target, "saved_preference"
    return "guangya", "product_default"


def validate_preference_updates(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or not arguments or set(arguments) - set(PREFERENCE_FIELDS):
        raise ValueError("媒体偏好字段无效或为空")
    result: dict[str, Any] = {}
    for key, raw in arguments.items():
        if key in _ENUMS:
            value = str(raw or "").strip().casefold()
            if value not in _ENUMS[key]:
                raise ValueError(f"{key} 的取值不受支持")
        elif key in _LIST_LIMITS:
            if not isinstance(raw, list) or len(raw) > _LIST_LIMITS[key]:
                raise ValueError(f"{key} 必须是最多 {_LIST_LIMITS[key]} 项的数组")
            value = []
            seen: set[str] = set()
            for item in raw:
                if not isinstance(item, str):
                    raise ValueError(f"{key} 中的匹配词必须是字符串")  # noqa: TRY004 - 偏好字段统一按无效值处理。
                term = unicodedata.normalize("NFKC", item).strip()
                if (not term or len(term) > _TERM_CHARS.get(key, 80) or
                        any(unicodedata.category(char).startswith("C") for char in term)):
                    raise ValueError(f"{key} 中包含无效匹配词")
                if key in {"preferred_genres", "excluded_genres"} and any(
                    not part.strip() for part in term.split("|")
                ):
                    raise ValueError(f"{key} 中包含空的同义匹配词")
                if key == "preferred_codecs":
                    term = term.casefold()
                    if term not in _CODECS:
                        raise ValueError("preferred_codecs 仅支持 h264、hevc、av1")
                if term.casefold() not in seen:
                    value.append(term)
                    seen.add(term.casefold())
        elif key == "exclude_played":
            if not isinstance(raw, bool):
                raise ValueError("exclude_played 必须是布尔值")
            value = raw
        else:
            maximum = 200 if key == "max_episode_size_gb" else 10
            if (isinstance(raw, bool) or not isinstance(raw, (int, float)) or
                    not 0 <= raw <= maximum or not math.isfinite(raw)):
                raise ValueError(f"{key} 必须是 0 到 {maximum} 的有限数字")
            value = float(raw)
        result[key] = value
    return result


def snapshot_media_preferences_change(
    current: dict[str, Any], updates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """按确切已读/已提交版本冻结字段合并；同一算法用于预检与 undo 写后证明。"""
    normalized = validate_preference_updates(updates) if updates is not None else None
    proposed = repository.merge_media_preferences(current, normalized) if normalized is not None else default_media_preferences()
    snapshot = {"current": current, "proposed": proposed} if normalized is not None else current
    fingerprint = hashlib.sha256(json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return {"current": current, "proposed": proposed, "updates": normalized, "fingerprint": fingerprint}


def prepare_media_preferences_change(
    owner_digest: str, updates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """读取完整当前版本并冻结字段合并；updates=None 表示恢复默认，预检不写入。"""
    if not isinstance(owner_digest, str) or not owner_digest.strip():
        raise ValueError("媒体偏好必须提供确切身份摘要")
    return snapshot_media_preferences_change(repository.get_media_preferences(owner_digest), updates)


def public_media_preferences(value: dict[str, Any]) -> dict[str, Any]:
    return {key: value[key] for key in (*PREFERENCE_FIELDS, "explicit") if key in value}


def media_preferences_preview(prepared: dict[str, Any]) -> dict[str, Any]:
    """共用业务预览；完整备忘逐条列出，不把私有 revision token 暴露给客户端。"""
    current = public_media_preferences(prepared["current"])
    if prepared["updates"] is None:
        return {"current": current, "defaults": prepared["proposed"]}
    changes = []
    for key, value in prepared["updates"].items():
        if key == "memory_notes":
            text = "".join(f"\n· {note}" for note in value) or "清空"
        elif isinstance(value, list):
            text = "、".join(value) or "清空"
        elif isinstance(value, bool):
            text = "是" if value else "否"
        else:
            text = _PREFERENCE_VALUES.get(str(value), str(value))
        changes.append(f"{_PREFERENCE_LABELS[key]}：{text}")
    return {"current": current, "proposed": prepared["proposed"], "effects": changes}


def apply_media_preferences_change(
    owner_digest: str, updates: dict[str, Any] | None = None, *, expected_fingerprint: str,
) -> dict[str, Any] | None:
    """比对原预检并复用仓储 revision/token CAS；返回自身提交状态，漂移返回 None。"""
    prepared = prepare_media_preferences_change(owner_digest, updates)
    if not secrets.compare_digest(prepared["fingerprint"], expected_fingerprint or ""):
        return None
    current = prepared["current"]
    expected = {"expected_revision": current["revision"], "expected_revision_token": current["revision_token"]}
    if prepared["updates"] is not None:
        return repository.set_media_preferences(owner_digest, updates=prepared["updates"], **expected)
    if repository.clear_media_preferences(owner_digest, **expected):
        # 删除无独立版本墓碑；仅返回已知缺失状态，不能用其跨 ABA 执行回退。
        return {**default_media_preferences(), "revision": 0, "explicit": False}
    return None


def effective_preferences(
    preferences: dict[str, Any], overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """字段级单次覆盖，包括显式空数组/0/False，不能被长期偏好覆盖。"""
    result = default_media_preferences()
    for key in PREFERENCE_FIELDS:
        if key in preferences:
            try:
                result.update(validate_preference_updates({key: preferences[key]}))
            except ValueError:
                pass  # 损坏的存量字段不应中断只读推荐。
    if overrides:
        result.update(validate_preference_updates(overrides))
    return result


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
    ranges = infer_dynamic_ranges(title)
    hdr = {"DoVi": "dolby_vision", "HDR10+": "hdr10+", "HDR10": "hdr10", "SDR": "sdr"}.get(ranges[0], "") if ranges else ""
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


def owner_media_preferences(owner: str) -> dict[str, Any]:
    owner = (owner or "").strip()
    if not owner:
        return default_media_preferences()
    return effective_preferences(get_media_preferences(media_owner_digest(owner)))


def owner_memory(owner: str) -> str:
    """当前身份的偏好和技能索引；技能正文按需读取，不作为系统指令灌入。"""
    from app.repositories.agent_skills import list_skills

    preferences = owner_media_preferences(owner)
    defaults = default_media_preferences()
    saved = {key: preferences[key] for key in PREFERENCE_FIELDS
             if key != "memory_notes" and preferences.get(key) != defaults.get(key)}
    notes = list(preferences.get("memory_notes") or [])
    skills = sorted(list_skills(owner=owner), key=lambda item: item["revision"], reverse=True)
    if not saved and not notes and not skills:
        return ""
    memory: dict[str, Any] = {"preferences": saved, "notes": notes}
    if skills:
        memory["skills"] = {
            "items": [{"name": item["name"], "revision": item["revision"],
                       "description": item["description"][:160],
                       **({"description_truncated": True} if len(item["description"]) > 160 else {})}
                      for item in skills[:20]],
            "total": len(skills), "list_method": "skills.list", "read_method": "skills.get",
        }
    return json.dumps(memory, ensure_ascii=False) + (
        "\n更新或忘记偏好时，用 preferences.update 提交需要更新的字段（memory_notes 提交完整新列表），或用 preferences.clear 清除全部偏好，用户确认后生效。" if saved or notes else ""
    )
