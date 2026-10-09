from __future__ import annotations

from collections.abc import Iterable
import re

from .errors import IndexerQueryRejected
from .models import MAX_SEARCH_TEXT_LENGTH, IndexerMediaSearchRequest

_HAN = re.compile(r"[\u3400-\u9fff]")
_JAPANESE_KANA = re.compile(r"[\u3040-\u30ff]")
_LATIN = re.compile(r"[A-Za-z]")
_POSITION_MARKER = re.compile(
    r"(?ix)(?:"
    r"(?<![a-z0-9])s\s*0*\d{1,3}(?:[ ._\-]*e\s*0*\d{1,4})?"
    r"|(?<![a-z0-9])e(?:p(?:isode)?)?[ ._\-]*0*\d{1,4}(?!\d)"
    r"|第\s*0*\d{1,3}\s*季"
    r"|第\s*0*\d{1,4}\s*[集話话]"
    r")"
)


def encode_empire_keyword(value: str) -> bytes:
    """两站帝国CMS限制的是GBK编码字节，不是Python字符数。"""
    try:
        encoded = value.encode("gbk")
    except UnicodeEncodeError as exc:
        raise IndexerQueryRejected("search term cannot be encoded as GBK") from exc
    if not 2 <= len(encoded) <= 20:
        raise IndexerQueryRejected("search term must contain 2 to 20 GBK bytes")
    return encoded


def _unique(values: Iterable[str], *, limit: int = 3) -> tuple[str, ...]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = " ".join(str(value or "").split())
        key = normalized.casefold()
        if not normalized or key in seen:
            continue
        seen.add(key)
        output.append(normalized)
        if len(output) == limit:
            break
    return tuple(output)


def _is_japanese(value: str) -> bool:
    return bool(_JAPANESE_KANA.search(value))


def _is_latin(value: str) -> bool:
    return bool(_LATIN.search(value)) and not _HAN.search(value) and not _is_japanese(value)


def _is_han_query(value: str) -> bool:
    return bool(_HAN.search(value)) and not _LATIN.search(value) and not _is_japanese(value)


def needs_bilingual_search(queries: tuple[str, ...]) -> bool:
    """Whether the first two queries are Latin and pure-Han variants, in either order."""

    if len(queries) < 2:
        return False
    first, second = queries[:2]
    if _is_japanese(first) or _is_japanese(second):
        return False
    return (_is_latin(first) and _is_han_query(second)) or (
        _is_han_query(first) and _is_latin(second)
    )


def _position_suffix(request: IndexerMediaSearchRequest) -> str:
    if request.episode is not None:
        if request.season is None:
            return f"E{request.episode:02d}"
        return f"S{request.season:02d}E{request.episode:02d}"
    if request.season is not None:
        return f"S{request.season:02d}"
    return ""


def _append_position_suffix(title: str, suffix: str) -> str:
    # 请求标题与排名证据保持原样；只为站点查询的季集后缀预留共享长度预算。
    prefix = title[:MAX_SEARCH_TEXT_LENGTH - len(suffix) - 1].rstrip()
    return f"{prefix} {suffix}"


def _with_position(title: str, request: IndexerMediaSearchRequest) -> str:
    normalized = " ".join(str(title or "").split())
    suffix = _position_suffix(request)
    if not normalized or not suffix or _POSITION_MARKER.search(normalized):
        return normalized
    return _append_position_suffix(normalized, suffix)


def _with_chinese_episode(title: str, request: IndexerMediaSearchRequest) -> str:
    normalized = " ".join(str(title or "").split())
    if not normalized or request.episode is None or _POSITION_MARKER.search(normalized):
        return normalized
    if request.season is not None:
        return _append_position_suffix(normalized, f"第{request.season}季 第{request.episode}集")
    return _append_position_suffix(normalized, f"第{request.episode}集")


def build_site_queries(site_id: str, request: IndexerMediaSearchRequest) -> tuple[str, ...]:
    """Return at most three stable, year-free query variants for one provider.

    When a caller provides season/episode intent, exact-position aliases are attempted before
    broad title aliases. This prevents an old but non-empty first page from suppressing the
    query that can actually find the requested release.
    """

    site_id = str(site_id or "").strip().lower()
    aliases = list(request.aliases)
    latin_aliases = [value for value in aliases if _is_latin(value)]
    other_aliases = [value for value in aliases if value not in latin_aliases]
    title = request.title
    original = request.original_title
    english = request.english_title

    if site_id == "kpkuang":
        # 目录索引按完整作品名称检索；年份/季集交由共享候选排序核对。
        return _unique((title, original, english, *aliases))

    if site_id in {"dygang", "ys5266"}:
        # 搜索的是作品目录，季集在详情资源中排序；不把SxxExx拼进片名。
        supported = []
        for value in (title, original, english, *aliases):
            try:
                encode_empire_keyword(value)
            except IndexerQueryRejected:
                continue
            supported.append(value)
        # 无合法完整名称时交由provider明确拒绝，不能截断片名或冒称无资源。
        return _unique(supported) or (title,)

    if site_id == "mikan":
        bases = [title, original, *aliases, english]
    elif site_id == "nyaa":
        latin_names = [value for value in (*aliases, original, english) if _is_latin(value)]
        pure_cjk_title = bool(_HAN.search(title)) and not _LATIN.search(title) and not _is_japanese(title)
        if pure_cjk_title and not _is_japanese(original) and latin_names:
            primary = latin_names[0]
            primary_positioned = _with_position(primary, request)
            broad_cjk = (
                original
                if _HAN.search(original) and not _LATIN.search(original) and not _is_japanese(original)
                else title
            )
            broad_latin = next(
                (value for value in latin_names[1:] if value.casefold() != primary.casefold()),
                primary if primary_positioned != primary else "",
            )
            # Nyaa release teams may use the original total episode number in Chinese titles.
            return _unique((primary_positioned, broad_cjk, broad_latin))
        preferred_latin = latin_aliases[:1]
        remaining_latin = latin_aliases[1:]
        bases = [*preferred_latin, original, *remaining_latin, english, title, *other_aliases]
    elif site_id == "btbtla":
        bases = [title, english, original, *aliases]
    elif site_id == "tpb":
        bases = [english, *latin_aliases]
        if _is_latin(original):
            bases.append(original)
        if _is_latin(title) or not any(bases):
            bases.append(title)
    elif site_id == "sukebei":
        bases = [original, *latin_aliases, english, title, *other_aliases]
    else:
        bases = [title, original, english, *aliases]

    bases = list(_unique(bases, limit=8))
    if request.season is not None or request.episode is not None:
        positioned = [_with_position(value, request) for value in bases]
        if site_id == "mikan" and request.episode is not None:
            primary = bases[0] if bases else title
            numbered = primary if _POSITION_MARKER.search(primary) else _append_position_suffix(primary, f"{request.episode:02d}")
            # 蜜柑发布名常用裸集号，不先耗费预算查两种不存在的季集写法。
            candidates = [numbered, primary, *positioned[1:], *bases[1:]]
        elif site_id == "btbtla" and request.episode is not None:
            primary = bases[0] if bases else title
            localized = _with_chinese_episode(primary, request)
            candidates = [
                _with_position(primary, request),
                localized,
                primary,
                *positioned[1:],
                *bases[1:],
            ]
        else:
            candidates = [*positioned, *bases]
        queries = _unique(candidates)
    else:
        queries = _unique(bases)
    if queries:
        return queries
    return (_with_position(title, request) or title,)
