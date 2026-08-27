from __future__ import annotations

import re
from collections.abc import Iterable

from .models import IndexerMediaSearchRequest

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


def _is_chinese(value: str) -> bool:
    return bool(_HAN.search(value)) and not _is_japanese(value)


def _is_latin(value: str) -> bool:
    return bool(_LATIN.search(value)) and not _HAN.search(value) and not _is_japanese(value)


def _position_suffix(request: IndexerMediaSearchRequest) -> str:
    if request.episode is not None:
        if request.season is None:
            return f"E{request.episode:02d}"
        return f"S{request.season:02d}E{request.episode:02d}"
    if request.season is not None:
        return f"S{request.season:02d}"
    return ""


def _with_position(title: str, request: IndexerMediaSearchRequest) -> str:
    normalized = " ".join(str(title or "").split())
    suffix = _position_suffix(request)
    if not normalized or not suffix or _POSITION_MARKER.search(normalized):
        return normalized
    return f"{normalized} {suffix}"


def _with_chinese_episode(title: str, request: IndexerMediaSearchRequest) -> str:
    normalized = " ".join(str(title or "").split())
    if not normalized or request.episode is None or _POSITION_MARKER.search(normalized):
        return normalized
    if request.season is not None:
        return f"{normalized} 第{request.season}季 第{request.episode}集"
    return f"{normalized} 第{request.episode}集"


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

    if site_id == "mikan":
        bases = [title, original, *aliases, english]
    elif site_id in {"nyaa", "animetosho"}:
        preferred_latin = latin_aliases[:1]
        remaining_latin = latin_aliases[1:]
        bases = [*preferred_latin, original, *remaining_latin, english, title, *other_aliases]
    elif site_id in {"1lou", "btbtla"}:
        bases = [title, english, original, *aliases]
    elif site_id == "tpb":
        bases = [english, *latin_aliases]
        if _is_latin(original):
            bases.append(original)
        if _is_latin(title):
            bases.append(title)
    elif site_id == "sukebei":
        bases = [original, *latin_aliases, english, title, *other_aliases]
    else:
        bases = [title, original, english, *aliases]

    bases = list(_unique(bases, limit=8))
    if request.season is not None or request.episode is not None:
        positioned = [_with_position(value, request) for value in bases]
        if site_id in {"1lou", "btbtla", "mikan"} and request.episode is not None:
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
