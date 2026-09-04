"""媒体服务器本地候选的确定性推荐排序。"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from typing import Any

from app.clients.base import MediaItem, MediaRecommendationCandidate

_JAPANESE_SCRIPT_RE = re.compile(r"[\u3040-\u30ff]")


def _normalized(value: object) -> str:
    return " ".join(
        unicodedata.normalize("NFKC", str(value or "")).casefold().split()
    )


def _compact(value: object) -> str:
    return "".join(char for char in _normalized(value) if char.isalnum())


def _expression_matches(text: str, compact_text: str, expression: str) -> bool:
    for raw in str(expression or "").split("|"):
        term = _normalized(raw)
        if not term:
            continue
        compact_term = _compact(term)
        if term in text or (compact_term and compact_term in compact_text):
            return True
    return False


def _candidate_corpus(candidate: MediaRecommendationCandidate) -> tuple[str, str]:
    values = [
        candidate.name,
        candidate.original_title,
        candidate.overview,
        *candidate.genres,
        *candidate.tags,
        *candidate.studios,
        *candidate.production_locations,
    ]
    if _JAPANESE_SCRIPT_RE.search(candidate.original_title):
        # 原始标题含平假名/片假名是可验证的日语元数据，只用于本地排序信号。
        values.extend(("日本", "Japanese", "日语"))
    text = _normalized(" ".join(str(value or "") for value in values))
    return text, _compact(text)


def _history_signals(
    history: list[MediaItem],
) -> tuple[set[str], set[str], Counter[str], list[str]]:
    object_ids: set[str] = set()
    titles: set[str] = set()
    genres: Counter[str] = Counter()
    recent_titles: list[str] = []
    seen_entities: set[str] = set()
    for item in history:
        object_id = str(item.series_id or item.id or "").strip()
        if object_id:
            object_ids.add(object_id)
        display_title = str(item.display_name or "").strip()
        title = _compact(display_title)
        if title:
            titles.add(title)
        entity_key = object_id or title
        if not entity_key or entity_key in seen_entities:
            continue
        seen_entities.add(entity_key)
        if display_title:
            recent_titles.append(display_title)
        for genre in item.genres:
            normalized = _normalized(genre)
            if normalized:
                genres[normalized] += 1
    return object_ids, titles, genres, recent_titles


def rank_local_recommendations(
    candidates: list[MediaRecommendationCandidate],
    history: list[MediaItem],
    *,
    must_match: list[str],
    prefer: list[str],
    exclude: list[str],
    min_rating: float,
    exclude_played: bool,
    limit: int,
) -> dict[str, Any]:
    """按显式条件、评分和历史题材偏好筛选；历史只作轻量排序信号。"""
    history_ids, history_titles, history_genres, history_recent_titles = (
        _history_signals(history)
    )
    ranked: list[tuple[float, MediaRecommendationCandidate, dict[str, Any]]] = []
    excluded_played_count = 0
    excluded_required_count = 0
    excluded_terms_count = 0
    excluded_rating_count = 0

    for candidate in candidates:
        if exclude_played and (
            candidate.watched
            or candidate.id in history_ids
            or _compact(candidate.name) in history_titles
        ):
            excluded_played_count += 1
            continue
        if candidate.community_rating < min_rating:
            excluded_rating_count += 1
            continue
        text, compact_text = _candidate_corpus(candidate)
        required_matches = [
            term
            for term in must_match
            if _expression_matches(text, compact_text, term)
        ]
        if len(required_matches) != len(must_match):
            excluded_required_count += 1
            continue
        if any(_expression_matches(text, compact_text, term) for term in exclude):
            excluded_terms_count += 1
            continue
        preferred_matches = [
            term for term in prefer if _expression_matches(text, compact_text, term)
        ]

        affinity_genres = [
            genre
            for genre in candidate.genres
            if _normalized(genre) in history_genres
        ]
        history_score = sum(
            min(history_genres[_normalized(genre)], 4) * 0.25
            for genre in affinity_genres
        )
        score = (
            candidate.community_rating * 1.5
            + candidate.critic_rating / 50.0
            + len(required_matches) * 8.0
            + len(preferred_matches) * 4.0
            + history_score
        )
        ranked.append(
            (
                score,
                candidate,
                {
                    "matched_required": required_matches,
                    "matched_preferences": preferred_matches,
                    "history_affinity_genres": affinity_genres[:6],
                },
            )
        )

    ranked.sort(
        key=lambda row: (
            row[0],
            row[1].community_rating,
            row[1].critic_rating,
            row[1].year,
            row[1].name,
        ),
        reverse=True,
    )
    selected = ranked[:limit]
    items = [
        {
            "__object_id": candidate.id,
            "__object_kind": "media_item",
            "name": candidate.name,
            "media_type": candidate.media_type,
            "year": candidate.year,
            "original_title": candidate.original_title,
            "community_rating": candidate.community_rating,
            "critic_rating": candidate.critic_rating,
            "genres": list(candidate.genres[:8]),
            "tags": list(candidate.tags[:12]),
            "overview": candidate.overview[:320],
            "matched_required": signals["matched_required"],
            "matched_preferences": signals["matched_preferences"],
            "history_affinity_genres": signals["history_affinity_genres"],
        }
        for _score, candidate, signals in selected
    ]
    return {
        "items": items,
        "count": len(items),
        "matched_count": len(ranked),
        "considered_count": len(candidates),
        "excluded": {
            "played_or_started": excluded_played_count,
            "required_terms": excluded_required_count,
            "excluded_terms": excluded_terms_count,
            "below_rating": excluded_rating_count,
        },
        "history": {
            "records_used": len(history),
            "unique_works_used": len(history_recent_titles),
            "genre_signals_used": len(history_genres),
        },
    }
