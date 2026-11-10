"""按明确媒体身份查询评分；优先豆瓣结构化数据，必要时受控核验豆瓣条目页。"""

from __future__ import annotations

import math
import re
import unicodedata
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from app import config
from app.concurrency import run_awaitable_sync
from app.modules.web_evidence import search_web
from app.clients.http import FixedHostHttpClient, HttpError
from app.discovery.models import (
    MediaCard, ProviderAuthenticationError, ProviderError, ProviderRateLimited, ProviderUnavailable,
)
from app.discovery.search import get_discovery_search_service
from app.discovery.service import get_discovery_service
from app.logger import get_logger
from app.sensitive_data import contains_sensitive_credential

logger = get_logger(__name__)
_ALLOWED_MEDIA_TYPES = {"movie", "tv"}
_SCORE_NEAR_LABEL_RE = re.compile(
    r"(?:豆瓣(?:电影)?评分|豆瓣评分|评分|rating)\s*(?:为|是|[:：])?\s*"
    r"([0-9](?:\.[0-9])?|10(?:\.0)?)",
    re.IGNORECASE,
)
_SCORE_BEFORE_LABEL_RE = re.compile(
    r"([0-9](?:\.[0-9])?|10(?:\.0)?)\s*(?:分)?\s*(?:豆瓣(?:电影)?评分|豆瓣评分)",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
_DOUBAN_SUBJECT_PATH_RE = re.compile(r"^/subject/\d+/?$")
_DOUBAN_HOST = "movie.douban.com"
_DOUBAN_PAGE_MAX_BYTES = 768 * 1024


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _visible_text(value: Any, *, limit: int) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip()
    cleaned = "".join(
        " " if unicodedata.category(char).startswith("C") else char
        for char in normalized
    )
    return " ".join(cleaned.split())[:limit]


def _match_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(char for char in text if char.isalnum())


def _media_type_label(media_type: str) -> str:
    return (
        "电视剧"
        if media_type == "tv"
        else "电影"
        if media_type == "movie"
        else "影视作品"
    )


def _douban_title_key(value: Any) -> str:
    title = _visible_text(value, limit=240)
    title = re.sub(r"\s*[（(]豆瓣[)）]\s*$", "", title, flags=re.IGNORECASE)
    return _match_text(title)


def _valid_rating(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        rating = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(rating) or not 0 <= rating <= 10:
        return None
    return round(rating, 1)


def _candidate_score(
    card: MediaCard,
    *,
    query_key: str,
    media_type: str,
    year: str,
) -> int | None:
    if media_type and card.media_type != media_type:
        return None
    if year and card.year and card.year != year:
        return None
    title_keys = {_match_text(card.title), _match_text(card.original_title)} - {""}
    if query_key in title_keys:
        score = 100
    elif len(query_key) >= 3 and any(query_key in value for value in title_keys):
        score = 45
    else:
        return None
    if media_type and card.media_type == media_type:
        score += 20
    if year and card.year == year:
        score += 15
    if card.provider == "douban":
        score += 5
    return score


def _select_card(
    cards: tuple[MediaCard, ...] | list[MediaCard],
    *,
    query: str,
    media_type: str,
    year: str,
) -> MediaCard | None:
    query_key = _match_text(query)
    ranked: list[tuple[int, int, MediaCard]] = []
    for index, card in enumerate(cards):
        score = _candidate_score(
            card,
            query_key=query_key,
            media_type=media_type,
            year=year,
        )
        if score is not None:
            ranked.append((score, -index, card))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    # 应用已有身份条件后仍并列的不同作品，不能靠搜索顺序猜测。
    identities = {(card.provider, card.media_type, card.external_id)
                  for score, _index, card in ranked if score == ranked[0][0]}
    if len(identities) > 1:
        return None
    return ranked[0][2]


def _success_result(
    card: MediaCard,
    rating: float,
    *,
    source_method: str,
    web_fallback_used: bool,
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    media_label = _media_type_label(card.media_type)
    year_text = f"（{card.year}，{media_label}）" if card.year else f"（{media_label}）"
    return {
        "ok": True,
        "status": "success",
        "summary": f"《{card.title}》{year_text}的豆瓣评分是 {rating:g} 分。",
        "data": {
            "query": card.title,
            "title": card.title,
            "original_title": card.original_title,
            "year": card.year,
            "media_type": card.media_type,
            "rating": rating,
            "rating_source": "douban",
            "source_method": source_method,
            "web_fallback_used": web_fallback_used,
        },
        "evidence": evidence,
        "suggestions": ["如果要继续检查缺集或更新，直接说“检查这部剧有没有缺集”。"],
    }


def _verified_douban_subject_url(value: Any) -> str | None:
    try:
        parsed = urlsplit(str(value or ""))
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").rstrip(".").lower() != _DOUBAN_HOST
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not _DOUBAN_SUBJECT_PATH_RE.fullmatch(parsed.path or "")
    ):
        return None
    return f"https://{_DOUBAN_HOST}{parsed.path}"


def _rating_from_douban_html(
    html: str,
    *,
    query: str,
    year: str,
    media_type: str = "",
) -> float | None:
    """只从身份匹配的豆瓣条目页提取公开数字评分，不回传页面正文。"""
    if not isinstance(html, str) or not html.strip():
        return None
    soup = BeautifulSoup(html, "html.parser")
    title_node = soup.select_one('meta[property="og:title"]')
    description_node = soup.select_one('meta[property="og:description"]')
    title_text = (
        str(title_node.get("content") or "")
        if title_node is not None
        else soup.title.get_text(" ", strip=True)
        if soup.title is not None
        else ""
    )
    description_text = (
        str(description_node.get("content") or "")
        if description_node is not None
        else ""
    )
    identity_text = _visible_text(f"{title_text} {description_text}", limit=1200)
    query_key = _match_text(query)
    if not query_key or _douban_title_key(title_text) != query_key:
        return None
    if year and year not in identity_text:
        return None
    lowered_identity = identity_text.casefold()
    if (
        media_type == "tv"
        and "电影" in lowered_identity
        and not any(token in lowered_identity for token in ("电视剧", "剧集", "电视"))
    ):
        return None
    if media_type == "movie" and any(
        token in lowered_identity for token in ("电视剧", "剧集")
    ):
        return None

    for selector in (
        '[property="v:average"]',
        '[itemprop="ratingValue"]',
        "strong.rating_num",
        'meta[itemprop="ratingValue"]',
    ):
        node = soup.select_one(selector)
        if node is None:
            continue
        raw_value = (
            node.get("content")
            if node.name == "meta"
            else node.get_text(" ", strip=True)
        )
        rating = _valid_rating(raw_value)
        if rating is not None:
            return rating
    return None


async def _fetch_douban_rating_async(
    url: str,
    *,
    query: str,
    year: str,
    media_type: str,
) -> float | None:
    verified_url = _verified_douban_subject_url(url)
    if verified_url is None:
        return None
    client = FixedHostHttpClient(
        allowed_hosts={_DOUBAN_HOST},
        timeout_seconds=8,
        max_response_bytes=_DOUBAN_PAGE_MAX_BYTES,
        max_redirects=0,
        user_agent="MediaFlux-Agent-Rating/1.0",
        pin_resolved_address=True,
    )
    try:
        response = await client.get(verified_url, max_redirects=0)
        if response.status_code in {401, 403}:
            raise ProviderAuthenticationError("豆瓣公开条目页要求登录或拒绝访问")
        if response.status_code == 429:
            raise ProviderRateLimited("豆瓣公开条目页限制访问频率")
        if response.status_code in {404, 410}:
            return None
        if response.status_code != 200:
            raise ProviderUnavailable("豆瓣公开条目页当前不可用")
        return _rating_from_douban_html(
            response.text,
            query=query,
            year=year,
            media_type=media_type,
        )
    finally:
        await client.aclose()


def _web_rating(
    *,
    query: str,
    media_type: str,
    year: str,
) -> tuple[tuple[float, str] | None, list[dict[str, Any]]]:
    # 没有类型时无法可靠地区分同名电影与剧集，宁可要求补充信息也不猜。
    if media_type not in _ALLOWED_MEDIA_TYPES:
        return None, []
    qualifiers = [query]
    if year:
        qualifiers.append(year)
    qualifiers.extend(
        [_media_type_label(media_type), "豆瓣评分", "site:movie.douban.com"]
    )
    result = search_web(" ".join(qualifiers), max_results=5)
    evidence = [{"source": "web_search", "status": result["status"], "ok": result["ok"],
                 "description": "已尝试网页补查。" if result["ok"] else f"已尝试网页补查，搜索未完成（{result['status']}）。",
                 "collected_at": _now()}]
    if not result["ok"]:
        return None, evidence
    query_key = _match_text(query)
    fetch_attempts = 0
    candidates = set()
    matches = {}
    for item in result["data"].get("results") or []:
        if not isinstance(item, dict):
            continue
        verified_url = _verified_douban_subject_url(item.get("url"))
        if verified_url is None:
            continue
        title = _visible_text(item.get("title"), limit=240)
        snippet = _visible_text(item.get("snippet"), limit=900)
        combined = f"{title} {snippet}"
        if not query_key or _douban_title_key(title) != query_key:
            continue
        lowered = combined.casefold()
        if (
            media_type == "tv"
            and "电影" in lowered
            and not any(token in lowered for token in ("电视剧", "剧集", "电视"))
        ):
            continue
        if media_type == "movie" and any(
            token in lowered for token in ("电视剧", "剧集")
        ):
            continue
        candidates.add(verified_url)
        match = _SCORE_NEAR_LABEL_RE.search(combined) or _SCORE_BEFORE_LABEL_RE.search(
            combined
        )
        if match and (not year or year in combined):
            rating = _valid_rating(match.group(1))
            if rating is not None:
                matches[verified_url] = (rating, "web_search")
                continue
        # 搜索摘要可能没有年份或分数；最多读取两个已验证条目页，并由页面元数据
        # 再次核对片名、年份和媒体类型，避免多个候选造成长时间串行等待。
        if fetch_attempts >= 2:
            continue
        fetch_attempts += 1
        try:
            rating = run_awaitable_sync(_fetch_douban_rating_async(
                verified_url, query=query, year=year, media_type=media_type,
            ))
        except (ProviderError, HttpError, httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
            status = "timeout" if isinstance(exc, (TimeoutError, httpx.TimeoutException)) else getattr(exc, "code", "unavailable")
            evidence.append({"source": "web_fetch", "url": verified_url, "status": status, "ok": False,
                             "description": f"豆瓣公开条目页读取未完成（{status}）。", "collected_at": _now()})
            continue
        evidence.append({"source": "web_fetch", "url": verified_url, "status": "success" if rating is not None else "not_found",
                         "ok": True, "description": "已读取豆瓣公开条目页并核对作品身份与评分。", "collected_at": _now()})
        if rating is not None:
            matches[verified_url] = (rating, "web_fetch")
    if not matches:
        return None, evidence
    if len(matches) > 1 or not year and len(candidates) > 1:
        evidence.append({"source": "web_search", "status": "ambiguous", "ok": True,
                         "description": "同名条目身份仍有歧义，未按搜索顺序选择。", "collected_at": _now()})
        return None, evidence
    selected_url, selected = next(iter(matches.items()))
    if selected[1] == "web_search":
        evidence[0]["url"] = selected_url
    return selected, evidence


def lookup_rating(
    query: str,
    media_type: str = "",
    year: str | int = "",
    *,
    allow_web_fallback: bool = True,
) -> dict[str, Any]:
    """查询豆瓣评分与实际来源；同名身份、失败和未查来源分别保留。"""
    if not isinstance(query, str):
        raise ValueError("query 必须是字符串")
    query = _visible_text(query, limit=121)
    if not query or len(query) > 120:
        raise ValueError("query 必须为 1 到 120 个可见字符")
    if contains_sensitive_credential(query):
        raise ValueError("联网查询内容不能包含凭据或密钥")

    raw_type = media_type
    if raw_type is None:
        raw_type = ""
    if not isinstance(raw_type, str):
        raise ValueError("media_type 必须是字符串")
    media_type = raw_type.strip().lower()
    if media_type and media_type not in _ALLOWED_MEDIA_TYPES:
        raise ValueError("media_type 仅支持 movie 或 tv")

    raw_year = year
    if raw_year is None:
        raw_year = ""
    if isinstance(raw_year, int) and not isinstance(raw_year, bool):
        raw_year = str(raw_year)
    if not isinstance(raw_year, str):
        raise ValueError("year 必须是四位年份")
    year = raw_year.strip()
    if year and not _YEAR_RE.fullmatch(year):
        raise ValueError("year 必须是 1900 到 2099 的四位年份")

    if not isinstance(allow_web_fallback, bool):
        raise ValueError("allow_web_fallback 必须是布尔值")
    selected: MediaCard | None = None
    provider_error = False
    web_fallback_attempted = False
    structured_attempted = config.get_bool("DISCOVERY_ENABLED")
    ambiguous = False
    evidence = [{"source": "douban", "status": "not_queried", "ok": None,
                 "description": "已尝试豆瓣结构化查询。" if structured_attempted else "结构化查询未启用，本次未调用。",
                 "collected_at": _now()}]

    if structured_attempted:
        try:
            search_result = get_discovery_search_service().search(query, 1, ["douban"])
            selected = _select_card(
                search_result.items,
                query=query,
                media_type=media_type,
                year=year,
            )
            provider_error = bool(search_result.errors) and not bool(
                search_result.providers_succeeded
            )
            ambiguous = selected is None and any(
                _candidate_score(card, query_key=_match_text(query), media_type=media_type, year=year) is not None
                for card in search_result.items
            )
            evidence[0].update(ok=not provider_error,
                               status=search_result.errors[0]["code"] if provider_error else
                               "ambiguous" if ambiguous else "available" if search_result.items else "empty")
        except Exception as exc:  # 辅助评分失败只报告该来源，不中断调用方。
            logger.warning("豆瓣评分结构化查询失败 type=%s", type(exc).__name__)
            provider_error = True
            evidence[0].update(ok=False, status=getattr(exc, "code", "unavailable"))

    if selected is not None:
        rating = _valid_rating(selected.rating)
        if rating is not None and (not year or selected.year == year):
            return _success_result(
                selected,
                rating,
                source_method="douban_search",
                web_fallback_used=False,
                evidence=evidence,
            )
        try:
            detail = get_discovery_service().get_detail(
                "douban", selected.media_type, selected.external_id
            )
        except Exception as exc:  # 详情不可用仍可按调用方授权尝试网页补查。
            logger.warning("豆瓣评分详情查询失败 type=%s", type(exc).__name__)
            detail = None
            provider_error = True
            evidence.append({"source": "douban_detail", "status": getattr(exc, "code", "unavailable"), "ok": False,
                             "description": "豆瓣结构化详情查询未完成。", "collected_at": _now()})
        if detail is not None:
            verified = ((detail.provider, detail.media_type, detail.external_id)
                        == (selected.provider, selected.media_type, selected.external_id)
                        and _candidate_score(detail, query_key=_match_text(query), media_type=media_type, year=year) is not None
                        and (not year or detail.year == year))
            evidence.append({"source": "douban_detail", "status": "available" if verified else "identity_mismatch", "ok": True,
                             "description": "已核对豆瓣结构化详情的作品身份和年份。", "collected_at": _now()})
            rating = _valid_rating(detail.rating)
            if rating is not None and verified:
                return _success_result(
                    detail,
                    rating,
                    source_method="douban_detail",
                    web_fallback_used=False,
                    evidence=evidence,
                )

    if allow_web_fallback and media_type in _ALLOWED_MEDIA_TYPES and not ambiguous:
        web_fallback_attempted = True
        web_match, web_evidence = _web_rating(query=query, media_type=media_type, year=year)
        evidence.extend(web_evidence)
        provider_error |= any(item["ok"] is False for item in web_evidence)
        ambiguous = any(item["status"] == "ambiguous" for item in web_evidence)
        if web_match is not None:
            rating, source_method = web_match
            fallback_card = MediaCard(
                provider="douban",
                external_id="web-search-result",
                media_type=media_type,
                title=query,
                year=year,
                rating=rating,
                rating_source="douban",
            )
            return _success_result(
                fallback_card,
                rating,
                source_method=source_method,
                web_fallback_used=True,
                evidence=evidence,
            )

    media_label = _media_type_label(media_type)
    identity = f"《{query}》"
    if year or media_type:
        qualifiers = "，".join(
            value for value in (year, media_label if media_type else "") if value
        )
        identity += f"（{qualifiers}）"
    if ambiguous:
        reason = "同名作品仍有歧义，尚未核实要查询哪一个条目"
        suggestions = ["请补充作品年份或更完整的片名；不会按搜索顺序猜选评分。"]
    elif not media_type:
        reason = "尚未明确要查询电影还是电视剧，无法核验评分"
        suggestions = ["请补充“电影”或“电视剧”，我会继续查询同一作品。"]
    else:
        reason = "豆瓣数据源暂时不可用" if provider_error else "未取得可核验的豆瓣评分"
        reason += (
            "；网页补查未取得可核验的评分"
            if web_fallback_attempted
            else "；未进行网页补查"
        )
        suggestions = ["稍后直接说“重试”，我会继续查询同一部作品。"]
    if not web_fallback_attempted:
        evidence[0]["description"] += "本次未进行网页补查。"
    return {
        "ok": False,
        "status": "ambiguous" if ambiguous else "unavailable" if provider_error else "not_found",
        "summary": f"{identity}{reason}。",
        "data": {
            "query": query,
            "title": selected.title if selected is not None else query,
            "original_title": selected.original_title if selected is not None else "",
            "year": selected.year if selected is not None else year,
            "media_type": selected.media_type if selected is not None else media_type,
            "rating": None,
            "rating_source": "douban",
            "source_method": "none",
            "web_fallback_used": web_fallback_attempted,
        },
        "evidence": evidence,
        "suggestions": suggestions,
        "error": "rating_ambiguous" if ambiguous else "rating_unavailable" if provider_error else "rating_not_found",
    }
