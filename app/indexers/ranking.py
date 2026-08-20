from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import replace
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Iterable

from app.modules.scraper import parse_release_position

from .models import IndexerItem, IndexerMediaSearchRequest

_SEPARATORS = re.compile(r"[^0-9a-z\u3400-\u9fff\u3040-\u30ff]+", re.IGNORECASE)
_YEAR = re.compile(r"(?<!\d)(18\d{2}|19\d{2}|20\d{2}|21\d{2}|2200)(?!\d)")
_BRACKET_GROUP = re.compile(r"[\[【(（][^\]】)）]{1,64}[\]】)）]")
_RELEASE_NOISE = re.compile(
    r"(?ix)\b(?:2160p|1080p|720p|480p|4k|uhd|hdr10\+?|dolby[ ._-]?vision|dv|"
    r"web[ ._-]?dl|webrip|bluray|bdrip|hdtv|remux|x26[45]|h26[45]|hevc|av1|"
    r"aac|flac|dts(?:-hd)?|truehd|atmos|10bit|8bit|proper|repack)\b"
)


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(_SEPARATORS.sub(" ", text).split())


def _evidence_values(media: IndexerMediaSearchRequest | None, fallback_query: str) -> tuple[str, ...]:
    if media is None:
        values: Iterable[str] = (fallback_query,)
    else:
        values = (media.title, media.original_title, media.english_title, *media.aliases)
    output: list[str] = []
    for value in values:
        normalized = _normalize(value)
        if normalized and normalized not in output:
            output.append(normalized)
    return tuple(output)


def rank_item(
    item: IndexerItem,
    *,
    media: IndexerMediaSearchRequest | None,
    fallback_query: str,
    now: datetime | None = None,
) -> IndexerItem:
    candidate = _normalize(item.title)
    evidences = _evidence_values(media, fallback_query)
    score = 0.0
    reasons: list[str] = []
    best_ratio = 0.0
    best_kind = ""
    for evidence in evidences:
        if not evidence:
            continue
        if candidate == evidence:
            ratio, kind = 1.0, "title_exact"
        elif evidence in candidate:
            ratio, kind = min(0.98, len(evidence) / max(len(candidate), 1) + 0.45), "title_contains"
        else:
            ratio, kind = SequenceMatcher(None, evidence, candidate).ratio(), "title_similar"
        if ratio > best_ratio:
            best_ratio, best_kind = ratio, kind
    if best_kind == "title_exact":
        score += 72
        reasons.append(best_kind)
    elif best_kind == "title_contains":
        score += 48 + 14 * best_ratio
        reasons.append(best_kind)
    elif best_ratio >= 0.55:
        score += 20 + 24 * best_ratio
        reasons.append(best_kind)
    elif best_ratio >= 0.35:
        score += 10 + 15 * best_ratio
        reasons.append("title_weak")

    request_year = media.year if media is not None else None
    title_years = {int(value) for value in _YEAR.findall(item.title)}
    if request_year and request_year in title_years:
        score += 8
        reasons.append("year_match")
    elif request_year and title_years and request_year not in title_years:
        score -= 8
        reasons.append("year_conflict")

    if item.download_state == "ready":
        score += 7
        reasons.append("download_ready")
    elif item.download_state == "resolvable":
        score += 4
        reasons.append("download_resolvable")

    if item.seeders is not None and item.seeders > 0:
        score += min(8.0, math.log1p(item.seeders) * 1.45)
        reasons.append("seeded")

    reference = now or datetime.now(timezone.utc)
    published = item.published_at
    if published is not None:
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (reference - published).total_seconds() / 86400)
        score += max(0.0, 4.0 - min(age_days, 365.0) / 365.0 * 4.0)
        if age_days <= 30:
            reasons.append("recent")

    return replace(
        item,
        relevance_score=max(0, min(100, round(score))),
        match_reasons=tuple(dict.fromkeys(reasons)),
    )


def _cluster_signature(title: str) -> str:
    position = parse_release_position(title)
    normalized = unicodedata.normalize("NFKC", str(title or "")).casefold()
    normalized = _BRACKET_GROUP.sub(" ", normalized)
    normalized = _RELEASE_NOISE.sub(" ", normalized)
    normalized = _YEAR.sub(" ", normalized)
    normalized = _normalize(normalized)
    if len(normalized.replace(" ", "")) < 4:
        return ""
    return "|".join(
        (
            normalized,
            f"s{position.get('season') if position.get('season') is not None else '-'}",
            f"e{position.get('episode') if position.get('episode') is not None else '-'}",
            f"x{position.get('episode_end') if position.get('episode_end') is not None else '-'}",
        )
    )


def annotate_clusters(items: list[IndexerItem]) -> list[IndexerItem]:
    signatures = [_cluster_signature(item.title) for item in items]
    counts: dict[str, int] = {}
    for signature in signatures:
        if signature:
            counts[signature] = counts.get(signature, 0) + 1
    output: list[IndexerItem] = []
    for item, signature in zip(items, signatures):
        if not signature or counts.get(signature, 0) <= 1:
            output.append(replace(item, cluster_id=None, cluster_size=1))
            continue
        cluster_id = "c_" + hashlib.sha256(signature.encode("utf-8")).hexdigest()[:12]
        output.append(replace(item, cluster_id=cluster_id, cluster_size=counts[signature]))
    return output
