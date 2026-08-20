from __future__ import annotations

import re
from collections.abc import Iterable

from .models import IndexerMediaSearchRequest

_HAN = re.compile(r"[\u3400-\u9fff]")
_JAPANESE_KANA = re.compile(r"[\u3040-\u30ff]")
_LATIN = re.compile(r"[A-Za-z]")


def _unique(values: Iterable[str], *, limit: int = 3) -> tuple[str, ...]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value or "").strip()
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


def build_site_queries(site_id: str, request: IndexerMediaSearchRequest) -> tuple[str, ...]:
    """Return at most three stable, year-free query variants for one provider."""

    site_id = str(site_id or "").strip().lower()
    aliases = list(request.aliases)
    latin_aliases = [value for value in aliases if _is_latin(value)]
    other_aliases = [value for value in aliases if value not in latin_aliases]
    title = request.title
    original = request.original_title
    english = request.english_title

    if site_id == "mikan":
        candidates = [title, original, *aliases, english]
    elif site_id in {"nyaa", "animetosho"}:
        preferred_latin = latin_aliases[:1]
        remaining_latin = latin_aliases[1:]
        candidates = [*preferred_latin, original, *remaining_latin, english, title, *other_aliases]
    elif site_id in {"1lou", "btbtla"}:
        candidates = [title, english, original, *aliases]
    elif site_id == "tpb":
        candidates = [english, *latin_aliases]
        if _is_latin(original):
            candidates.append(original)
        if _is_latin(title):
            candidates.append(title)
    elif site_id == "sukebei":
        candidates = [original, *latin_aliases, english, title, *other_aliases]
    else:
        candidates = [title, original, english, *aliases]

    queries = _unique(candidates)
    if queries:
        return queries
    return (title,)
