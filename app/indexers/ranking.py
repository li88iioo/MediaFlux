from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime, timezone
from difflib import SequenceMatcher

from app.modules.episode_mapping import classify_episode_position

from .models import IndexerItem, IndexerMediaSearchRequest
from .release import parse_indexer_release_position

_SEPARATORS = re.compile(r"[^0-9a-z\u3400-\u9fff\u3040-\u30ff]+", re.IGNORECASE)
_HAN_ONLY = re.compile(r"^[\u3400-\u9fff]+$")
_HAN_CHAR = re.compile(r"[\u3400-\u9fff]")
_YEAR = re.compile(r"(?<!\d)(18\d{2}|19\d{2}|20\d{2}|21\d{2}|2200)(?!\d)")
_TECHNICAL_YEAR_VALUE = re.compile(
    r"(?<!\d)\d{3,5}\s*[x×]\s*\d{3,5}(?!\d)"
    r"|(?<!\d)\d+(?:[.,]\d+)?\s*(?:[kmgt]?(?:bps|b/s|bits?/s))(?![a-z])"
    r"|(?<![a-z0-9])(?:480|576|720|1080|1440|1920|2160|4320)\s*[pi](?![a-z0-9])",
    re.IGNORECASE,
)
_BRACKET_GROUP = re.compile(r"[\[【(（]([^\]】)）]{1,80})[\]】)）]")
_RELEASE_POSITION = re.compile(
    r"(?ix)(?:"
    r"(?<![a-z0-9])s(?:eason)?[ ._\-]*0*\d{1,3}(?:[ ._\-]*e[ ._\-]*0*\d{1,4}(?:\s*(?:-|~|～|to|至)\s*e?[ ._\-]*0*\d{1,4})?)?"
    r"|(?<!\d)0*\d{1,3}\s*x\s*0*\d{1,4}(?:\s*(?:-|~|～|to|至)\s*0*\d{1,4})?"
    r"|第\s*0*\d{1,3}\s*季"
    r"|第\s*0*\d{1,4}\s*(?:-|~|～|至)\s*(?:第\s*)?0*\d{1,4}\s*[集話话]"
    r"|第\s*0*\d{1,4}\s*[集話话]"
    r"|(?:全集|全)\s*0*\d{1,4}\s*[集話话]"
    r")"
)
_RELEASE_NOISE = re.compile(
    r"(?ix)\b(?:2160p|1080p|720p|480p|4k|uhd|hdr10\+?|dolby[ ._-]?vision|dv|"
    r"web[ ._-]?dl|webrip|bluray|bdrip|hdtv|remux|x26[45]|h26[45]|hevc|av1|avc|"
    r"aac(?:2\.0)?|flac|dts(?:-hd)?(?:5\.1)?|truehd|atmos|10bit|8bit|60fps|proper|repack|"
    r"torrent|mkv|mp4)\b"
)
_CLUSTER_NOISE = re.compile(
    r"(?ix)(?:国语(?:配音|音轨)?|中文字幕|简繁字幕|简体|繁体|中字|高码版|杜比视界版本|"
    r"60帧率版本|国漫|动漫|动画|complete|batch|fin(?:ished)?|gb|chs|cht|aac|dts)"
)
_LEADING_GROUP = re.compile(r"^\s*[\[【(（]([^\]】)）]{1,80})[\]】)）]")


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


def _contains_evidence(raw_candidate: str, normalized_candidate: str, evidence: str) -> bool:
    if evidence not in normalized_candidate:
        return False
    compact = evidence.replace(" ", "")
    if not (_HAN_ONLY.fullmatch(compact) and len(compact) <= 4):
        return True

    raw = unicodedata.normalize("NFKC", str(raw_candidate or "")).casefold()
    for match in re.finditer(re.escape(compact), raw):
        left = raw[match.start() - 1] if match.start() else ""
        right = raw[match.end()] if match.end() < len(raw) else ""
        if not _HAN_CHAR.fullmatch(left or " ") and not _HAN_CHAR.fullmatch(right or " "):
            return True
    return False


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
        elif _contains_evidence(item.title, candidate, evidence):
            ratio, kind = min(0.98, len(evidence) / max(len(candidate), 1) + 0.45), "title_contains"
        else:
            ratio, kind = SequenceMatcher(None, evidence, candidate).ratio(), "title_similar"
        if ratio > best_ratio:
            best_ratio, best_kind = ratio, kind
    if best_kind == "title_exact":
        score += 72
        reasons.append(best_kind)
    elif best_kind == "title_contains":
        score += 56 + 14 * best_ratio
        reasons.append(best_kind)
    elif best_ratio >= 0.55:
        score += 20 + 24 * best_ratio
        reasons.append(best_kind)
    elif best_ratio >= 0.35:
        score += 10 + 15 * best_ratio
        reasons.append("title_weak")

    request_year = media.year if media is not None else None
    year_text = _TECHNICAL_YEAR_VALUE.sub(" ", item.title)
    title_years = {int(value) for value in _YEAR.findall(year_text)}
    if request_year and request_year in title_years:
        score += 9
        reasons.append("year_match")
    elif request_year and title_years and request_year not in title_years:
        score -= 18
        reasons.append("year_conflict")

    if media is not None and (media.season is not None or media.episode is not None):
        position = parse_indexer_release_position(item.title, media_title=media.title)
        position_match = classify_episode_position(
            source_season=position.get("season"),
            source_episode=position.get("episode"),
            source_episode_end=position.get("episode_end"),
            target_season=media.season,
            target_episode=media.episode,
        ).relation
        if position_match == "exact":
            score += 18
            reasons.append("episode_exact")
        elif position_match == "range":
            score += 12
            reasons.append("episode_range")
        elif position_match == "season":
            score += 6
            reasons.append("season_match")
        elif position_match == "conflict":
            score -= 32
            reasons.append("episode_conflict")

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


def match_priority(item: IndexerItem) -> int:
    """作品身份先于热度/画质；不删弱匹配，留给用户手动核验。"""
    reasons = set(item.match_reasons)
    if reasons & {"year_conflict", "episode_conflict"}:
        return 3
    if reasons & {"title_exact", "title_contains"}:
        return 0 if "year_match" in reasons else 1
    return 2 if "title_similar" in reasons else 3


def _meaningful_bracket(match: re.Match[str]) -> str:
    content = match.group(1)
    normalized = _normalize(content)
    if not normalized or _YEAR.fullmatch(normalized) or normalized.isdigit():
        return " "
    cleaned = _RELEASE_NOISE.sub(" ", normalized)
    cleaned = _CLUSTER_NOISE.sub(" ", cleaned)
    cleaned = _normalize(cleaned)
    if not cleaned or len(cleaned.replace(" ", "")) < 2:
        return " "
    return f" {cleaned} "


def _cluster_signature(title: str) -> str:
    position = parse_indexer_release_position(title)
    normalized = unicodedata.normalize("NFKC", str(title or "")).casefold()
    leading = _LEADING_GROUP.match(normalized)
    if leading is not None:
        group = _normalize(leading.group(1))
        # Release-group labels differ between sites and should not split otherwise identical releases.
        if "-" in leading.group(1) or re.fullmatch(r"[a-z0-9 ]{1,20}", group):
            normalized = normalized[leading.end():]
    normalized = _BRACKET_GROUP.sub(_meaningful_bracket, normalized)
    normalized = _RELEASE_POSITION.sub(" ", normalized)
    normalized = _RELEASE_NOISE.sub(" ", normalized)
    normalized = _CLUSTER_NOISE.sub(" ", normalized)
    normalized = _YEAR.sub(" ", normalized)
    normalized = re.sub(r"\b\d+(?:\.\d+)?\s*[kmgtpe]?i?b\b", " ", normalized, flags=re.IGNORECASE)
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


def published_timestamp(value: datetime | None) -> float:
    if value is None:
        return -1.0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()

def match_order(item: IndexerItem) -> tuple[int, int]:
    if "episode_exact" in item.match_reasons:
        position_priority = 0
    elif "episode_range" in item.match_reasons:
        position_priority = 1
    elif "season_match" in item.match_reasons:
        position_priority = 2
    elif "episode_conflict" in item.match_reasons:
        position_priority = 4
    else:
        position_priority = 3
    return position_priority, match_priority(item)

def candidate_sort_key(
    entry: tuple[int, int, IndexerItem],
    sort_mode: str,
) -> tuple[object, ...]:
    site_index, provider_index, item = entry
    if sort_mode == "source_order":
        return site_index, provider_index
    relevance = int(item.relevance_score or 0)
    seeders = int(item.seeders if item.seeders is not None else -1)
    size = item.size_bytes
    published_value = published_timestamp(item.published_at)
    position = parse_indexer_release_position(item.title)
    season = position.get("season")
    episode = position.get("episode")
    episode_end = position.get("episode_end") or episode
    priority = match_order(item)
    stable = (-relevance, -seeders, -published_value, site_index, provider_index)
    if sort_mode == "published_desc":
        return (
            *priority,
            -published_value,
            -relevance,
            -seeders,
            site_index,
            provider_index,
        )
    if sort_mode == "episode_desc":
        return (
            *priority,
            -(season if season is not None else -1),
            -(episode_end if episode_end is not None else -1),
            -(episode if episode is not None else -1),
            *stable,
        )
    if sort_mode == "seeders_desc":
        return (
            *priority,
            -seeders,
            -relevance,
            -published_value,
            site_index,
            provider_index,
        )
    if sort_mode == "size_desc":
        return (
            *priority,
            size is None,
            -(size if size is not None else 0),
            *stable,
        )
    if sort_mode == "size_asc":
        return (
            *priority,
            size is None,
            size if size is not None else 0,
            *stable,
        )
    return (*priority, *stable)
