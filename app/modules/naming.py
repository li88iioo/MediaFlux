"""受限的媒体命名模板渲染器。兼容 JMTE 风格 ${var} 与 {var} 占位符。"""
from __future__ import annotations

import re
from dataclasses import dataclass

_INVALID_NAME = re.compile(r'[\\/:*?"<>|]')
_TOKEN = re.compile(r"\$\{([A-Za-z][A-Za-z0-9_]*)\}|\{([A-Za-z][A-Za-z0-9_]*)\}")

MOVIE_DEFAULT = "${showTitle}.${showYear}${mediaInfoDotSuffix}.${ext}"
TV_DEFAULT = "${showTitle}.${showYear}.${seasonEpisode}${mediaInfoSuffix}.${ext}"
MOVIE_DIR_DEFAULT = "${showTitle}${yearSuffix} ${identityTag}"
SHOW_DIR_DEFAULT = "${showTitle}${yearSuffix} ${identityTag}"
NSFW_DIR_DEFAULT = "${showTitle}${yearSuffix}"

_ALIASES = {
    "showTitle": "title",
    "showYear": "year",
    "yearSuffix": "year_suffix",
    "showTmdb": "tmdb_id",
    "tmdbTag": "tmdb_tag",
    "identityId": "identity_id",
    "identityTag": "identity_tag",
    "seasonNr": "season",
    "episodeNr": "episode",
    "season_episode": "season_episode",
    "seasonEpisode": "season_episode",
    "mediaInfo": "media_info",
    "mediaInfoSuffix": "media_info_suffix",
    "mediaInfoDotSuffix": "media_info_dot_suffix",
    "originalName": "original_name",
    "originalStem": "original_stem",
}
_ALLOWED = {
    "title", "year", "year_suffix", "tmdb_id", "tmdb_tag", "identity_id", "identity_tag",
    "season", "episode", "season_episode",
    "media_info", "media_info_suffix", "media_info_dot_suffix", "ext", "original_name", "original_stem",
}


@dataclass(frozen=True)
class NamingContext:
    title: str = ""
    year: str = ""
    tmdb_id: str = ""
    tmdb_tag: str = ""
    identity_id: str = ""
    identity_tag: str = ""
    season: str = ""
    episode: str = ""
    season_episode: str = ""
    media_info: str = ""
    media_info_suffix: str = ""
    media_info_dot_suffix: str = ""
    ext: str = "mkv"
    original_name: str = ""
    original_stem: str = ""

    @property
    def year_suffix(self) -> str:
        return f" ({self.year})" if self.year else ""

    def values(self) -> dict[str, str]:
        return {name: str(getattr(self, name) or "") for name in _ALLOWED}


def validate_template(template: str) -> None:
    raw = (template or "").strip()
    if not raw:
        raise ValueError("命名模板不能为空")
    if len(raw) > 500:
        raise ValueError("命名模板不能超过 500 个字符")
    fields = []
    for match in _TOKEN.finditer(raw):
        fields.append(match.group(1) or match.group(2))
    unknown = sorted({field for field in fields if _ALIASES.get(field, field) not in _ALLOWED})
    if unknown:
        raise ValueError(f"不支持的模板变量: {', '.join(unknown[:5])}")
    residue = _TOKEN.sub("", raw)
    if "${" in residue or "{" in residue or "}" in residue:
        raise ValueError("模板占位符格式不正确")


def template_has_media_identity(
    template: str,
    *,
    tmdb_id: str | None = None,
    identity_id: str | None = None,
) -> bool:
    """目录模板是否会实际输出稳定媒体身份。

    不传身份值时保留原有的模板语法检测；整理计划会传入当前媒体身份，
    防止 MetaTube 匹配把空的 ``tmdbTag`` 误当作有效身份标识。
    """
    raw = (template or "")
    value_aware = tmdb_id is not None or identity_id is not None
    for match in _TOKEN.finditer(raw):
        field = match.group(1) or match.group(2)
        normalized = _ALIASES.get(field, field)
        if normalized in {"identity_id", "identity_tag"} and (
            not value_aware or bool(identity_id)
        ):
            return True
        if normalized in {"tmdb_id", "tmdb_tag"} and (
            not value_aware or bool(tmdb_id)
        ):
            return True
    return False


def _clean_name(value: str) -> str:
    name = _INVALID_NAME.sub("_", (value or "")).strip().rstrip(".")
    if not name or name in {".", ".."}:
        raise ValueError("模板渲染结果为空")
    return name


def _utf8_prefix(value: str, limit: int) -> str:
    # 沿用 240 的保守命名上限，但按文件系统实际字节计算，不切断中文码点。
    return value.encode("utf-8")[:max(0, limit)].decode("utf-8", errors="ignore")


def sanitize_name(value: str) -> str:
    return _utf8_prefix(_clean_name(value), 240).rstrip(".")


def render_template(template: str, context: NamingContext) -> str:
    """目录/通用模板：只压缩自由标题，保留年份、稳定身份等结构字段。"""
    validate_template(template)
    values = context.values()

    def replace(match: re.Match) -> str:
        field = match.group(1) or match.group(2)
        return values[_ALIASES.get(field, field)]

    raw = _TOKEN.sub(replace, template)
    rendered = _clean_name(raw)
    if len(rendered.encode("utf-8")) <= 240:
        return rendered
    fields = [_ALIASES.get(match.group(1) or match.group(2), match.group(1) or match.group(2))
              for match in _TOKEN.finditer(template)]
    flexible: dict[str, tuple[str, str, int]] = {}
    for field in ("title", "original_stem", "original_name"):
        count = fields.count(field)
        value = _INVALID_NAME.sub("_", values[field])
        if not count or not value:
            continue
        stem, suffix = value, ""
        if field == "original_name" and "." in value:
            stem, extension = value.rsplit(".", 1)
            suffix = "." + extension
        flexible[field] = (stem, suffix, count)
        values[field] = suffix
    # 原文件名的扩展名和模板中的固定/结构变量均计入必要尾部，绝不粗截它们。
    fixed = _INVALID_NAME.sub("_", _TOKEN.sub(replace, template))
    count = sum(item[2] for item in flexible.values())
    budget = (240 - len(fixed.encode("utf-8"))) // count if count else 0
    if budget < 1:
        raise ValueError("命名模板的必要尾部超过文件名长度上限")
    for field, (stem, suffix, _count) in flexible.items():
        shortened = _utf8_prefix(stem, budget)
        if stem and not shortened:
            raise ValueError("命名可变部分无法在长度上限内保留")
        values[field] = shortened + suffix
    return _clean_name(_TOKEN.sub(replace, template))


def fit_media_filename(value: str, *, protected_suffix: str = "") -> str:
    """文件名独立于目录截断：保留扩展名以及季集/CD/年份后的结构尾部。"""
    name = _clean_name(value)
    if len(name.encode("utf-8")) <= 240:
        return name
    stem, separator, ext = name.rpartition(".")
    if not separator or not stem or not ext:
        raise ValueError("媒体文件名缺少真实扩展名")
    suffix_start = len(stem) - len(protected_suffix) if protected_suffix and stem.endswith(protected_suffix) else len(stem)
    for pattern in (
        r"(?i)[._ -]S[0-9]+(?:E[0-9]+)?(?=[._ -]|$)",
        r"(?i)[._ -]CD[0-9]{1,2}(?=[._ -]|$)",
        r"[._ -](?:19|20)[0-9]{2}(?=[._ -]|$)",
    ):
        matches = list(re.finditer(pattern, stem))
        if matches:
            suffix_start = min(suffix_start, matches[-1].start())
    suffix = stem[suffix_start:] + "." + ext
    budget = 240 - len(suffix.encode("utf-8"))
    if budget < 1:
        raise ValueError("媒体必要尾部超过文件名长度上限")
    prefix = _utf8_prefix(stem[:suffix_start], budget).rstrip(" .")
    if not prefix:
        raise ValueError("媒体标题无法在长度上限内保留")
    return prefix + suffix


def render_media_template(template: str, context: NamingContext) -> str:
    """媒体模板必须保留来源的真实扩展名，不能把截断结果当成新扩展名。"""
    rendered = render_template(template, context)
    if not rendered.casefold().endswith("." + context.ext.casefold()):
        raise ValueError("媒体命名模板未保留真实扩展名")
    return fit_media_filename(rendered)


def append_variant_tags(name: str, tags: tuple[str, ...] | list[str]) -> str:
    """在扩展名前追加稳定版本标签，并保留 240 字符命名上限。"""
    safe_name = _INVALID_NAME.sub("_", (name or "")).strip().rstrip(".")
    if not safe_name or safe_name in {".", ".."}:
        raise ValueError("模板渲染结果为空")
    if "." in safe_name:
        stem, ext = safe_name.rsplit(".", 1)
        extension = f".{ext}"
    else:
        stem, extension = safe_name, ""
    existing = {part.lower() for part in re.split(r"[._ -]+", stem) if part}
    stable_tags: list[str] = []
    for raw in tags:
        tag = _INVALID_NAME.sub("_", (raw or "")).strip(" .")
        if tag and tag.lower() not in existing and tag.lower() not in {item.lower() for item in stable_tags}:
            stable_tags.append(tag)
    if not stable_tags:
        return fit_media_filename(safe_name)
    suffix = "." + ".".join(stable_tags)
    return fit_media_filename(f"{stem}{suffix}{extension}", protected_suffix=suffix)


def build_context(*, title: str, year: str | int, tmdb_id: str | int = "",
                  identity_id: str | int = "", identity_tag: str = "",
                  season=None, episode=None, media_info: str = "",
                  ext: str = "mkv", original_name: str = "") -> NamingContext:
    season_text = f"{int(season):02d}" if season is not None and str(season) != "" else ""
    episode_text = f"{int(episode):02d}" if episode is not None and str(episode) != "" else ""
    season_episode = f"S{season_text}" if season_text else ""
    if episode_text:
        season_episode += f"E{episode_text}"
    original_stem = original_name.rsplit(".", 1)[0] if "." in original_name else original_name
    # 上游可能只把标题中点改成形似字符；统一归档名称，不改元数据或原文件名。
    safe_title = _INVALID_NAME.sub("_", (title or "").replace("‧", "·"))
    tmdb_value = str(tmdb_id or "")
    stable_id = str(identity_id or tmdb_value)
    tmdb_tag = f"{{tmdb-{tmdb_value}}}" if tmdb_value else ""
    stable_tag = (identity_tag or tmdb_tag)
    return NamingContext(
        title=safe_title,
        year=str(year or ""),
        tmdb_id=tmdb_value,
        tmdb_tag=tmdb_tag,
        identity_id=stable_id,
        identity_tag=stable_tag,
        season=season_text,
        episode=episode_text,
        season_episode=season_episode,
        media_info=(media_info or ""),
        media_info_suffix=f"-{media_info}" if media_info else "",
        media_info_dot_suffix=f".{media_info}" if media_info else "",
        ext=(ext or "mkv").lstrip("."),
        original_name=(original_name or ""),
        original_stem=original_stem,
    )
