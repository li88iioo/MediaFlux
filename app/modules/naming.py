"""受限的媒体命名模板渲染器。兼容 JMTE 风格 ${var} 与 {var} 占位符。"""
from __future__ import annotations

import re
from dataclasses import dataclass

_INVALID_NAME = re.compile(r'[\\/:*?"<>|]')
_TOKEN = re.compile(r"\$\{([A-Za-z][A-Za-z0-9_]*)\}|\{([A-Za-z][A-Za-z0-9_]*)\}")

MOVIE_DEFAULT = "${showTitle}.${showYear}${mediaInfoDotSuffix}.${ext}"
TV_DEFAULT = "${showTitle}.${showYear}.${seasonEpisode}${mediaInfoSuffix}.${ext}"
MOVIE_DIR_DEFAULT = "${showTitle} (${showYear}) ${identityTag}"
SHOW_DIR_DEFAULT = "${showTitle} (${showYear}) ${identityTag}"

_ALIASES = {
    "showTitle": "title",
    "showYear": "year",
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
    "title", "year", "tmdb_id", "tmdb_tag", "identity_id", "identity_tag",
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

    def values(self) -> dict[str, str]:
        return {name: str(getattr(self, name) or "") for name in _ALLOWED}


def validate_template(template: str) -> None:
    raw = str(template or "").strip()
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
    raw = str(template or "")
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


def sanitize_name(value: str) -> str:
    name = _INVALID_NAME.sub("_", str(value or "")).strip().rstrip(".")
    if not name or name in {".", ".."}:
        raise ValueError("模板渲染结果为空")
    return name[:240]


def render_template(template: str, context: NamingContext) -> str:
    validate_template(template)
    values = context.values()

    def replace(match: re.Match) -> str:
        field = match.group(1) or match.group(2)
        return values[_ALIASES.get(field, field)]

    return sanitize_name(_TOKEN.sub(replace, template))


def append_variant_tags(name: str, tags: tuple[str, ...] | list[str]) -> str:
    """在扩展名前追加稳定版本标签，并保留 240 字符命名上限。"""
    safe_name = _INVALID_NAME.sub("_", str(name or "")).strip().rstrip(".")
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
        tag = _INVALID_NAME.sub("_", str(raw or "")).strip(" .")
        if tag and tag.lower() not in existing and tag.lower() not in {item.lower() for item in stable_tags}:
            stable_tags.append(tag)
    if not stable_tags:
        return safe_name
    suffix = "." + ".".join(stable_tags)
    stem_limit = max(1, 240 - len(suffix) - len(extension))
    return f"{stem[:stem_limit].rstrip('.')}{suffix}{extension}"


def build_context(*, title: str, year: str, tmdb_id: str = "",
                  identity_id: str = "", identity_tag: str = "",
                  season=None, episode=None, media_info: str = "",
                  ext: str = "mkv", original_name: str = "") -> NamingContext:
    season_text = f"{int(season):02d}" if season is not None and str(season) != "" else ""
    episode_text = f"{int(episode):02d}" if episode is not None and str(episode) != "" else ""
    season_episode = f"S{season_text}" if season_text else ""
    if episode_text:
        season_episode += f"E{episode_text}"
    original_stem = original_name.rsplit(".", 1)[0] if "." in original_name else original_name
    safe_title = _INVALID_NAME.sub("_", str(title or ""))
    tmdb_value = str(tmdb_id or "")
    stable_id = str(identity_id or tmdb_value)
    tmdb_tag = f"{{tmdb-{tmdb_value}}}" if tmdb_value else ""
    stable_tag = str(identity_tag or tmdb_tag)
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
        media_info=str(media_info or ""),
        media_info_suffix=f"-{media_info}" if media_info else "",
        media_info_dot_suffix=f".{media_info}" if media_info else "",
        ext=str(ext or "mkv").lstrip("."),
        original_name=str(original_name or ""),
        original_stem=original_stem,
    )
