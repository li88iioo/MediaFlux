"""缺集资源站候选的确定性质量排序与只读下载建议。"""
from __future__ import annotations

import math
import re
import unicodedata
from typing import Any

from app.indexers.release import parse_indexer_release_position
from app.modules.scraper import TMDBScraper

_MAX_ITEMS = 50
_MAX_REASON_LENGTH = 80


def _safe_text(value: Any, maximum: int) -> str:
    return " ".join(str(value or "").split())[:maximum]


def _safe_nonnegative(value: Any, maximum: int = 10**9) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, min(int(value), maximum))
    except (TypeError, ValueError, OverflowError):
        return None


def _normalized_title(value: Any) -> str:
    return unicodedata.normalize("NFKC", _safe_text(value, 300)).casefold()


def _episode_positions(title: str) -> set[tuple[int, int]]:
    positions: set[tuple[int, int]] = set()
    patterns = (
        r"(?<![a-z0-9])s\s*0*(\d{1,3})[ ._\-]*e\s*0*(\d{1,4})(?!\d)",
        r"(?<!\d)0*(\d{1,3})\s*x\s*0*(\d{1,4})(?!\d)",
        r"第\s*0*(\d{1,3})\s*季.{0,16}?第\s*0*(\d{1,4})\s*[集話话]",
    )
    for pattern in patterns:
        for raw_season, raw_episode in re.findall(pattern, title, flags=re.IGNORECASE):
            try:
                season = int(raw_season)
                episode = int(raw_episode)
            except (TypeError, ValueError):
                continue
            if 0 <= season <= 100 and 1 <= episode <= 1000:
                positions.add((season, episode))
    return positions


def _episode_ranges(title: str) -> list[tuple[int, int, int]]:
    ranges: list[tuple[int, int, int]] = []
    patterns = (
        r"(?<![a-z0-9])s\s*0*(\d{1,3})[ ._\-]*e\s*0*(\d{1,4})\s*(?:-|~|～|to|至)\s*e?\s*0*(\d{1,4})(?!\d)",
        r"(?<!\d)0*(\d{1,3})\s*x\s*0*(\d{1,4})\s*(?:-|~|～|to|至)\s*0*(\d{1,4})(?!\d)",
        r"第\s*0*(\d{1,3})\s*季.{0,16}?第\s*0*(\d{1,4})\s*(?:-|~|～|至)\s*0*(\d{1,4})\s*[集話话]",
    )
    for pattern in patterns:
        for raw_season, raw_start, raw_end in re.findall(
            pattern, title, flags=re.IGNORECASE
        ):
            try:
                parsed = int(raw_season), int(raw_start), int(raw_end)
            except (TypeError, ValueError):
                continue
            season, start, end = parsed
            start, end = sorted((start, end))
            if 0 <= season <= 100 and 1 <= start <= end <= 1000:
                ranges.append((season, start, end))
    return ranges


def _looks_like_season_pack(title: str, season: int) -> bool:
    escaped = str(season)
    patterns = (
        rf"(?<![a-z0-9])s\s*0*{escaped}(?!\d)",
        rf"\bseason\s*0*{escaped}(?!\d)",
        rf"第\s*0*{escaped}\s*季",
    )
    has_season = any(re.search(pattern, title, flags=re.IGNORECASE) for pattern in patterns)
    pack_marker = bool(re.search(
        r"complete|全集|全\s*\d+\s*[集話话]|season\s*pack|batch|合集",
        title,
        flags=re.IGNORECASE,
    ))
    return has_season and pack_marker


def _episode_match(
    title: str, *, season: int, episode: int
) -> tuple[str, int, list[str], list[str]]:
    parsed = parse_indexer_release_position(title)
    parsed_season = parsed.get("season")
    parsed_episode = parsed.get("episode")
    parsed_end = parsed.get("episode_end") or parsed_episode
    positions = _episode_positions(title)
    ranges = _episode_ranges(title)
    target = (season, episode)
    label = f"S{season:02d}E{episode:02d}"
    if parsed_season == season and parsed_episode is not None and parsed_end is not None:
        if parsed_episode == episode and parsed_end == episode:
            return "exact_episode", 220, [f"精确匹配 {label}"], []
    if _looks_like_season_pack(title, season):
        return (
            "season_pack",
            85,
            [f"识别为第 {season} 季整季资源"],
            ["整季包需人工核对文件清单"],
        )
    if parsed_season == season and parsed_episode is not None and parsed_end is not None:
        if parsed_episode <= episode <= parsed_end:
            return (
                "episode_pack",
                110,
                [f"资源范围包含 {label}（E{parsed_episode:02d}-E{parsed_end:02d}）"],
                ["多集资源需人工核对文件清单"],
            )
    if parsed_season is not None and parsed_season != season:
        parsed_label = f"S{parsed_season:02d}"
        if parsed_episode is not None:
            parsed_label += f"E{parsed_episode:02d}"
        return "conflict", -320, [], [f"季集标记与目标 {label} 冲突：{parsed_label}"]
    if parsed_episode is not None and parsed_end is not None and not (
        parsed_episode <= episode <= parsed_end
    ):
        parsed_label = f"E{parsed_episode:02d}"
        if parsed_end != parsed_episode:
            parsed_label += f"-E{parsed_end:02d}"
        return "conflict", -320, [], [f"季集标记与目标 {label} 冲突：{parsed_label}"]
    matching_range = next((
        (start, end)
        for range_season, start, end in ranges
        if range_season == season and start <= episode <= end
    ), None)
    if matching_range is not None:
        start, end = matching_range
        return (
            "episode_pack",
            110,
            [f"资源范围包含 {label}（E{start:02d}-E{end:02d}）"],
            ["多集资源需人工核对文件清单"],
        )
    if target in positions:
        return "exact_episode", 220, [f"精确匹配 {label}"], []
    if positions or ranges:
        samples = ", ".join(f"S{s:02d}E{e:02d}" for s, e in sorted(positions)[:3])
        if not samples and ranges:
            samples = ", ".join(
                f"S{s:02d}E{start:02d}-E{end:02d}"
                for s, start, end in ranges[:3]
            )
        return "conflict", -320, [], [f"季集标记与目标 {label} 冲突：{samples}"]
    return "unknown", 20, [], [f"标题未明确标出 {label}，需人工复核"]


def _tag_score(title: str) -> tuple[int, dict[str, str], list[str]]:
    raw_tags = TMDBScraper.parse_resource_tags(title)
    tags = {key: _safe_text(value, 64) for key, value in raw_tags.items() if value}
    reasons: list[str] = []
    score = 0

    resolution = tags.get("resolution", "")
    resolution_scores = {"2160p": 30, "1080p": 22, "720p": 12, "SD": 4}
    if resolution in resolution_scores:
        score += resolution_scores[resolution]
        reasons.append(resolution)

    media = tags.get("media", "")
    media_scores = {"Remux": 30, "BluRay": 24, "WEB-DL": 20, "WEBRip": 14, "HDTV": 8}
    if media in media_scores:
        score += media_scores[media]
        reasons.append(media)

    effects = tags.get("effect", "")
    if "DoVi" in effects:
        score += 8
        reasons.append("Dolby Vision")
    elif effects:
        score += 5
        reasons.append(effects)

    audio = tags.get("audio", "")
    if any(label in audio for label in ("Atmos", "TrueHD", "DTS-HD MA")):
        score += 5
        reasons.append("高规格音轨")
    elif audio:
        score += 2

    codec = tags.get("video_codec", "")
    if codec == "H.265":
        score += 5
    elif codec == "AV1":
        score += 4

    if re.search(r"简(?:体|中)|chs|gb(?:2312|k)?|中字|简繁|繁简", title, re.IGNORECASE):
        score += 8
        reasons.append("含简体中文标记")
    elif re.search(r"繁(?:體|体|中)|cht|big5", title, re.IGNORECASE):
        score += 4
        reasons.append("含繁体中文标记")

    return score, tags, reasons


def _availability_score(item: dict[str, Any]) -> tuple[int, bool, list[str], list[str]]:
    state = _safe_text(item.get("download_state"), 24).casefold()
    result_id = _safe_text(item.get("result_id"), 128)
    raw_kinds = item.get("download_kinds", [])
    if not isinstance(raw_kinds, (list, tuple, set)):
        raw_kinds = []
    kinds = [kind for kind in raw_kinds if kind in {"magnet", "torrent"}]
    reasons: list[str] = []
    warnings: list[str] = []
    if state == "ready":
        score = 45
        reasons.append("可直接提交下载")
    elif state == "resolvable":
        score = 30
        reasons.append("可在提交前解析下载地址")
    else:
        score = -120
        warnings.append("该结果当前不可提交下载")
    valid_result_id = bool(re.fullmatch(r"[A-Za-z0-9_-]{16,128}", result_id))
    eligible = bool(valid_result_id and kinds and state in {"ready", "resolvable"})
    if not valid_result_id:
        warnings.append("缺少可验证的短期资源句柄")
    if state in {"ready", "resolvable"} and not kinds:
        warnings.append("未声明可用下载类型，提交时仍需重新校验")
    return score, eligible, reasons, warnings


def _activity_score(item: dict[str, Any]) -> tuple[int, list[str], list[str]]:
    seeders = _safe_nonnegative(item.get("seeders"))
    downloads = _safe_nonnegative(item.get("downloads"))
    score = 0
    reasons: list[str] = []
    warnings: list[str] = []
    if seeders is None:
        warnings.append("站点未提供做种数")
    elif seeders == 0:
        score -= 5
        warnings.append("当前做种数为 0")
    else:
        score += min(18, int(round(math.log2(seeders + 1) * 3)))
        reasons.append(f"{seeders} 个做种")
    if downloads:
        score += min(6, int(round(math.log2(downloads + 1))))
    return score, reasons, warnings


def _bounded_messages(values: list[str], maximum: int) -> list[str]:
    result: list[str] = []
    for value in values:
        text = _safe_text(value, _MAX_REASON_LENGTH)
        if text and text not in result:
            result.append(text)
        if len(result) >= maximum:
            break
    return result


def _ranked_item(item: dict[str, Any], *, season: int, episode: int) -> dict[str, Any]:
    public = dict(item)
    title = _safe_text(public.get("title"), 300)
    match, match_score, match_reasons, match_warnings = _episode_match(
        title, season=season, episode=episode
    )
    availability_score, downloadable, availability_reasons, availability_warnings = (
        _availability_score(public)
    )
    tag_score, tags, tag_reasons = _tag_score(title)
    activity_score, activity_reasons, activity_warnings = _activity_score(public)
    eligible = downloadable and match != "conflict"
    score = match_score + availability_score + tag_score + activity_score
    if match == "exact_episode" and eligible:
        confidence = "high"
    elif eligible and match in {"episode_pack", "season_pack", "unknown"}:
        confidence = "medium" if match in {"episode_pack", "season_pack"} else "low"
    else:
        confidence = "low"
    public["quality"] = {
        "rank": 0,
        "score": int(score),
        "confidence": confidence,
        "match": match,
        "eligible": eligible,
        "reasons": _bounded_messages(
            match_reasons + availability_reasons + tag_reasons + activity_reasons, 6
        ),
        "warnings": _bounded_messages(
            match_warnings + availability_warnings + activity_warnings, 4
        ),
        "tags": tags,
    }
    return public


def _sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    quality = item["quality"]
    match_order = {
        "exact_episode": 4,
        "episode_pack": 3,
        "season_pack": 2,
        "unknown": 1,
        "conflict": 0,
    }
    seeders = _safe_nonnegative(item.get("seeders"))
    downloads = _safe_nonnegative(item.get("downloads"))
    return (
        -int(bool(quality["eligible"])),
        -int(quality["score"]),
        -match_order.get(str(quality["match"]), 0),
        -(seeders if seeders is not None else -1),
        -(downloads if downloads is not None else -1),
        _normalized_title(item.get("title")),
        _safe_text(item.get("site_id"), 32).casefold(),
        _safe_text(item.get("result_id"), 128),
    )


def _candidate_summary(item: dict[str, Any]) -> dict[str, Any]:
    quality = item["quality"]
    return {
        "result_id": _safe_text(item.get("result_id"), 128),
        "title": _safe_text(item.get("title"), 300),
        "site_id": _safe_text(item.get("site_id"), 32),
        "site_name": _safe_text(item.get("site_name"), 80),
        "rank": int(quality["rank"]),
        "score": int(quality["score"]),
        "confidence": str(quality["confidence"]),
        "match": str(quality["match"]),
        "download_state": _safe_text(item.get("download_state"), 24),
        "reasons": list(quality.get("reasons") or [])[:6],
        "warnings": list(quality.get("warnings") or [])[:4],
        "tags": dict(quality.get("tags") or {}),
    }


def rank_episode_search(
    search_data: dict[str, Any],
    *,
    season: int,
    episode: int,
) -> dict[str, Any]:
    """返回带排序解释和只读下载建议的搜索数据副本。"""
    ranked_data = dict(search_data)
    raw_items = search_data.get("items", [])
    items = [
        _ranked_item(item, season=season, episode=episode)
        for item in raw_items[:_MAX_ITEMS]
        if isinstance(item, dict)
    ] if isinstance(raw_items, list) else []
    items.sort(key=_sort_key)
    candidate_position = 0
    for rank, item in enumerate(items, start=1):
        item["quality"]["rank"] = rank
        item.pop("position", None)
        if item["quality"]["eligible"]:
            candidate_position += 1
            item["position"] = candidate_position
    ranked_data["items"] = items

    eligible = [item for item in items if item["quality"]["eligible"]]
    selected = eligible[0] if eligible else None
    if selected is None:
        status = "empty" if not items else "no_downloadable_candidate"
        reason = "没有可安全提交的候选资源。" if items else "本次搜索没有返回候选资源。"
    elif selected["quality"]["match"] == "exact_episode":
        status = "recommended"
        reason = "首选项明确匹配目标季集，且具备可验证的提交句柄。"
    else:
        status = "review_required"
        reason = "候选项未能明确匹配目标单集，提交前需要人工核对。"

    selected_summary = _candidate_summary(selected) if selected else None
    ranked_data["recommendation"] = {
        "status": status,
        "candidate_count": len(eligible),
        "selected": selected_summary,
        "alternatives": [_candidate_summary(item) for item in eligible[1:4]],
        "reason": reason,
    }
    ranked_data["download_plan"] = {
        "mode": "read_only",
        "auto_submit": False,
        "requires_confirmation": True,
        "prepare_tool": "indexer.submit_candidate",
        "supported_targets": ["qb", "guangya", "both"],
        "candidate_position": 1 if selected_summary else None,
        "note": "当前仅生成建议；提交前将按当前会话候选序号重新校验资源和下载目标。",
    }
    return ranked_data
