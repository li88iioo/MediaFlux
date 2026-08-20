"""剧集特别篇目录与命名的共享语义。"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_SPECIAL_DIR = re.compile(
    r"(?i)^(?:extras?|specials?|sps?|ova|oad|bonus|featurettes?|"
    r"特别篇|番外篇?|特典|花絮)$"
)
# TMDB 标记可以临时追加在目录或文件名末尾。特别篇判定必须先忽略
# 这些身份标记，否则 ``Extra tmdb123``、``Clean Opening ... tmdb123``
# 会退化为普通视频，并进一步把 TV ID 误送到 movie 详情端点。这里与
# scraper 的合法语法保持同样的边界，但不导入 scraper，避免循环依赖。
_EXPLICIT_TMDB_WRAPPED_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"(?:\{tmdb-[0-9]{1,10}\}|\(tmdb-[0-9]{1,10}\))"
    r"(?![A-Za-z0-9])"
)
_EXPLICIT_TMDB_BARE_RE = re.compile(
    r"(?i)(?:^|(?<=[\s._()\[\]{}【】]))"
    r"(?:tmdb(?:[ +\-]?)[0-9]{1,10}|tdmb(?:[ +\-]+)[0-9]{1,10})"
    r"(?=$|(?=[\s._()\[\]{}【】]))"
)
_SPECIAL_MEDIA_NAME = re.compile(
    r"(?i)(?:^|[ ._\-\[\(【])(?:omnibus|picture[ ._-]*drama|"
    r"pvs?[ ._-]*cms?|ova|oav|oad|specials?|sps?)(?=$|[ ._\-\]\)】])"
)
_SP_SPECIAL_POSITION = re.compile(
    r"(?i)(?<![a-z0-9])sp(?:[ ._-]*(?P<episode>\d{1,3}))?(?![a-z0-9])"
)
# 小数集号通常是正片之间插入的番外/短篇（1.5、4.5、7.5）。它们不能
# 原样表达为 Jellyfin/TMDB 的整数 SxxExx，但可以在同一作品作用域内按原始
# 数值顺序稳定映射到 Season 00。规则只接受明确季集、EP、发布名末尾或括号
# 位置，避免把容量 ``1.5GB``、版本 ``v1.5`` 和年份小数误判成集号。
_FRACTIONAL_EPISODE_PATTERNS = (
    re.compile(
        r"(?i)(?<![a-z0-9])s(?P<season>\d{1,3})[ ._-]*"
        r"e(?:p)?[ ._-]*(?P<episode>\d{1,4}\.\d{1,2})(?![a-z0-9])"
    ),
    re.compile(
        r"(?i)(?<![a-z0-9])e(?:p)?[ ._-]*"
        r"(?P<episode>\d{1,4}\.\d{1,2})(?![a-z0-9])"
    ),
    re.compile(
        r"(?i)(?:^|\s-\s)(?P<episode>\d{1,4}\.\d{1,2})"
        r"(?=$|[ ._\-\[\(【])"
    ),
    re.compile(
        r"(?i)[\[【(（]\s*(?P<episode>\d{1,4}\.\d{1,2})\s*[\]】)）]"
    ),
)
# 蓝光/整季发布常把片头片尾、映像特典和菜单视频与正片平铺在根目录。
# 只接受完整素材词、明确编号以及其后的技术发布标签。最后一项很重要：
# ``A Normal Movie - Menu 2 (2026)`` 仍可能是合法片名，不能仅凭 ``Menu 2``
# 就自动归入 Specials。
_RELEASE_SPECIAL_SUFFIX = re.compile(
    r"(?ix)(?:^|\s-\s)(?:"
    r"clean[ ._-]+openings?|clean[ ._-]+closings?|"
    r"extras?[ ._-]+\d{1,3}|"
    r"image[ ._-]+vocals?[ ._-]+\d{1,3}|"
    r"menus?[ ._-]+\d{1,3}"
    r")(?P<trailing>\s+(?:\[[^\]]+\]\s*)+)$"
)
_RELEASE_SPECIAL_TECHNICAL_TAG = re.compile(
    r"(?ix)\b(?:bd(?:rip)?|blu[ ._-]?ray|dvd(?:rip)?|web[ ._-]?dl|"
    r"x26[45]|h[ ._-]?26[45]|hevc|avc|\d{3,4}p|4k|uhd|10[ ._-]?bit)\b"
)
_ZERO_EPISODE_SPECIAL = re.compile(
    r"(?i)(?<![a-z0-9])(?:s\d{1,3}[ ._-]*)?e(?:p)?[ ._-]*0{1,3}(?!\d)"
)
_SEASON_ZERO_SPECIAL = re.compile(
    r"(?i)(?<![a-z0-9])s0{1,3}[ ._-]*e(?:p)?[ ._-]*(?P<episode>\d{1,3})(?!\d)"
)
_PROLOGUE_SPECIAL = re.compile(
    r"(?i)(?:^|[ ._\-\[\(【])prologue(?=$|[ ._\-\]\)】])"
)
_NC_SPECIAL_POSITION = re.compile(
    r"(?i)(?<![a-z0-9])(?P<kind>ncop|nced)"
    r"(?:[ ._-]*(?P<episode>\d{1,3}))?(?![a-z0-9])"
)
_BARE_OP_ED_POSITION = re.compile(
    r"(?i)(?:^|[\[\(【]|\s-\s)(?P<kind>op|ed)"
    r"(?:[ ._-]*(?P<episode>\d{1,3}))?(?![a-z0-9])"
)
_GENERIC_CONTEXT_DIR = re.compile(
    r"(?i)^(?:movies?|films?|tv|shows?|series|电影|影片|剧集|电视剧|动漫|纪录片|综艺|"
    r"season\s*\d+|s\d+|第\s*\d+\s*季|disc\s*\d+|cd\s*\d+|"
    r"720p|1080p|2160p|4k|uhd)$"
)


def _strip_explicit_tmdb_markers(value: str) -> str:
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
    for start, end in sorted(spans, reverse=True):
        text = f"{text[:start]} {text[end:]}"
    return re.sub(r"\s+", " ", text).strip()


def split_path(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[\\/]+", str(value or "")) if part.strip()]


def is_special_directory_name(value: str) -> bool:
    markerless = _strip_explicit_tmdb_markers(value)
    return bool(_SPECIAL_DIR.fullmatch(markerless.strip()))


def is_special_path(value: str) -> bool:
    return any(is_special_directory_name(part) for part in split_path(value))


def _media_stem(value: str) -> str:
    """只剥离真实媒体扩展名，保留 ``07.5`` 这类小数集号。"""
    name = str(value or "").rsplit("/", 1)[-1]
    base, separator, suffix = name.rpartition(".")
    if separator and re.fullmatch(r"(?i)[a-z][a-z0-9]{1,7}", suffix):
        return base
    return name


def special_media_position(value: str) -> int | None:
    """返回特别篇显式编号；无编号时交给调用方稳定分配。"""
    stem = _media_stem(value)
    stem = _strip_explicit_tmdb_markers(stem)
    season_zero = _SEASON_ZERO_SPECIAL.search(stem)
    if season_zero:
        number = int(season_zero.group("episode"))
        return number if 1 <= number <= 999 else 1
    if _ZERO_EPISODE_SPECIAL.search(stem) or _PROLOGUE_SPECIAL.search(stem):
        return 1
    for pattern in (_SP_SPECIAL_POSITION, _NC_SPECIAL_POSITION, _BARE_OP_ED_POSITION):
        match = pattern.search(stem)
        if not match:
            continue
        raw = match.groupdict().get("episode")
        if not raw:
            return None
        number = int(raw)
        return number if 1 <= number <= 999 else None
    return None


def fixed_special_media_position(value: str) -> int | None:
    """返回必须保留的显式 Season 00/E00 位置。

    NCOP2、NCED1 等编号描述的是素材变体，不是绝对的 Season 00 槽位；
    它们仍应按同目录的稳定扫描顺序参与冲突仲裁。
    """
    stem = _strip_explicit_tmdb_markers(_media_stem(value))
    season_zero = _SEASON_ZERO_SPECIAL.search(stem)
    if season_zero:
        number = int(season_zero.group("episode"))
        return number if 1 <= number <= 999 else 1
    if _ZERO_EPISODE_SPECIAL.search(stem):
        return 1
    return None


def fractional_episode_position(value: str) -> tuple[int | None, Decimal] | None:
    """返回小数集号的来源季与精确位置，用于稳定的 S00 顺序映射。"""
    stem = _media_stem(value)
    stem = _strip_explicit_tmdb_markers(stem)
    for pattern in _FRACTIONAL_EPISODE_PATTERNS:
        match = pattern.search(stem)
        if not match:
            continue
        raw_episode = str(match.groupdict().get("episode") or "").strip()
        try:
            episode = Decimal(raw_episode)
        except InvalidOperation:
            continue
        if episode <= 0 or episode >= 10000:
            continue
        raw_season = str(match.groupdict().get("season") or "").strip()
        season = int(raw_season) if raw_season else None
        return season, episode
    return None


def strip_special_media_markers(value: str) -> str:
    """移除特殊集位置标记，避免它们残留在作品标题中。"""
    cleaned = _ZERO_EPISODE_SPECIAL.sub(" ", str(value or ""))
    cleaned = _PROLOGUE_SPECIAL.sub(" ", cleaned)
    for pattern in _FRACTIONAL_EPISODE_PATTERNS:
        cleaned = pattern.sub(" ", cleaned)
    return cleaned


def is_special_media_name(value: str) -> bool:
    """识别 NCOP/NCED/OP/ED/OVA 等明确非正片素材，避免按电影自动归档。"""
    stem = _strip_explicit_tmdb_markers(_media_stem(value))
    release_special = _RELEASE_SPECIAL_SUFFIX.search(stem)
    return bool(
        _SPECIAL_MEDIA_NAME.search(stem)
        or _SP_SPECIAL_POSITION.search(stem)
        or _SEASON_ZERO_SPECIAL.search(stem)
        or _ZERO_EPISODE_SPECIAL.search(stem)
        or _PROLOGUE_SPECIAL.search(stem)
        or _NC_SPECIAL_POSITION.search(stem)
        or _BARE_OP_ED_POSITION.search(stem)
        or fractional_episode_position(stem) is not None
        or (
            release_special
            and _RELEASE_SPECIAL_TECHNICAL_TAG.search(
                release_special.group("trailing") or ""
            )
        )
    )


def has_fractional_episode_position(value: str) -> bool:
    """返回发布名是否含可稳定映射到 Season 00 的小数集号。"""
    return fractional_episode_position(value) is not None


def strip_special_directories(value: str) -> str:
    """移除路径中的特别篇容器，保留所属作品上下文。"""
    return "/".join(part for part in split_path(value) if not is_special_directory_name(part))


def special_parent_context(value: str, fallback: str = "") -> str:
    """返回特别篇容器所属作品侧的上下文，不采用容器后的素材分类目录。"""
    parts = split_path(value)
    for index, part in enumerate(parts):
        if is_special_directory_name(part):
            return "/".join(parts[:index]) or str(fallback or "").strip()
    return "/".join(parts) or str(fallback or "").strip()


def title_hint_from_path(value: str, fallback: str = "") -> str:
    """从作品路径中取最后一个非分类/季目录作为识别标题。"""
    for part in reversed(split_path(value)):
        if is_special_directory_name(part) or _GENERIC_CONTEXT_DIR.fullmatch(part):
            continue
        return part
    return str(fallback or "").strip()
