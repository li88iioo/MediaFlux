"""媒体库质量的有界只读检查；缺字段保持 unknown，不将一页样本冒充全库。"""

from __future__ import annotations

from collections import Counter
from typing import Any

from app.agent.provider_models import ProviderGatewayError, ProviderPayload


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _resolution(stream: dict[str, Any]) -> int | None:
    width = _nonnegative_int(stream.get("Width"))
    height = _nonnegative_int(stream.get("Height"))
    if not width or not height:
        return None
    # 1920x800、3840x1600 等宽银幕内容不应被误判为低清。
    return next(
        (level for level, w in ((2160, 3800), (1080, 1900), (720, 1260)) if width >= w),
        height,
    )


def inspect_quality_item(
    item: dict[str, Any], *, min_resolution: int, subtitle_language: str
) -> dict[str, Any]:
    streams = item.get("MediaStreams")
    sources = item.get("MediaSources")
    streams = streams if isinstance(streams, list) else []
    source_list = (
        [value for value in sources if isinstance(value, dict)]
        if isinstance(sources, list)
        else []
    )
    all_streams = [value for value in streams if isinstance(value, dict)]
    for source in source_list:
        source_streams = source.get("MediaStreams")
        if isinstance(source_streams, list):
            all_streams.extend(
                value for value in source_streams if isinstance(value, dict)
            )
    videos = [value for value in all_streams if value.get("Type") == "Video"]
    subtitles = [value for value in all_streams if value.get("Type") == "Subtitle"]
    heights = [value for video in videos if (value := _resolution(video)) is not None]
    resolution = max(heights) if heights else None
    if subtitle_language == "any":
        subtitle_state = (
            "present"
            if subtitles or item.get("HasSubtitles") is True
            else "missing"
            if item.get("HasSubtitles") is False
            else "unknown"
        )
    else:
        languages = {
            str(value.get("Language") or "").strip().casefold() for value in subtitles
        }
        chinese = {
            "chi",
            "zho",
            "zh",
            "zh-cn",
            "zh-tw",
            "zh-hans",
            "zh-hant",
            "chs",
            "cht",
            "cmn",
            "yue",
        }
        subtitle_state = (
            "present"
            if languages & chinese
            else "missing"
            if item.get("HasSubtitles") is False
            or languages
            and not languages & {"", "und", "unknown"}
            else "unknown"
        )
    version_count = _nonnegative_int(item.get("MediaSourceCount"))
    if version_count is None and isinstance(sources, list):
        version_count = len(source_list)
    sizes = [_nonnegative_int(source.get("Size")) for source in source_list]
    size_bytes = (
        sum(value for value in sizes if value is not None)
        if sizes and all(value is not None for value in sizes)
        else None
    )
    issues = []
    if resolution is not None and resolution < min_resolution:
        issues.append("低于目标清晰度")
    if subtitle_state == "missing":
        issues.append("缺少目标字幕")
    if version_count is not None and version_count > 1:
        issues.append("多个媒体版本")
    availability = (
        "missing"
        if item.get("IsMissing") is True or item.get("LocationType") == "Virtual"
        else "unknown"
    )
    if availability == "missing":
        issues.append("服务器报告缺失")
    return {
        "__object_id": str(item.get("Id") or ""),
        "__object_kind": "media_item",
        "name": str(item.get("Name") or "未命名条目"),
        "type": str(item.get("Type") or ""),
        "year": _nonnegative_int(item.get("ProductionYear")),
        "resolution": resolution,
        "resolution_state": "unknown"
        if resolution is None
        else "低于目标"
        if resolution < min_resolution
        else "达到目标",
        "subtitle_state": subtitle_state,
        "version_count": version_count,
        "size_bytes": size_bytes,
        "availability": availability,
        "issues": issues,
    }


def inspect_library_quality(
    client: Any,
    user_id: str,
    arguments: dict[str, Any],
    *,
    server_label: str,
    source: str,
) -> ProviderPayload:
    start = int(arguments.get("start_index", 0))
    limit = int(arguments.get("limit", 20))
    if start < 0 or not 1 <= limit <= 25:
        raise ProviderGatewayError("质量检查分页参数无效", code="invalid_arguments")
    if arguments.get("min_resolution", 1080) not in {720, 1080, 2160} or arguments.get(
        "subtitle_language", "chinese"
    ) not in {"any", "chinese"}:
        raise ProviderGatewayError("质量检查条件不受支持", code="invalid_arguments")
    params = {
        "UserId": user_id,
        "Recursive": "true",
        "IncludeItemTypes": "Movie,Episode",
        "Fields": "MediaSources,MediaStreams,ProviderIds",
        "EnableImages": "false",
        "EnableUserData": "false",
        "EnableTotalRecordCount": "true",
        "SortBy": "SortName,ProductionYear",
        "SortOrder": "Ascending",
        "StartIndex": start,
        "Limit": limit,
    }
    if arguments.get("library_ref"):
        params["ParentId"] = str(arguments["library_ref"])
    raw = client._request("/Items", params=params)
    if not isinstance(raw, dict) or not isinstance(raw.get("Items"), list):
        raise ProviderGatewayError(
            "媒体库质量检查返回格式无效", code="invalid_response"
        )
    received = raw["Items"]
    if len(received) > limit or any(
        not isinstance(item, dict) or not item.get("Id") for item in received
    ):
        raise ProviderGatewayError(
            "媒体服务器未遵守分页或缺少条目标识", code="invalid_response"
        )
    total = _nonnegative_int(raw.get("TotalRecordCount"))
    count = len(received)
    inconsistent = total is not None and (
        total < start + count or not count and start < total
    )
    if inconsistent:
        raise ProviderGatewayError(
            "媒体库在分页期间发生变化，请重新开始检查", code="provider_snapshot_changed"
        )
    has_more = start + count < total if total is not None else count == limit
    items = [
        inspect_quality_item(
            item,
            min_resolution=int(arguments.get("min_resolution", 1080)),
            subtitle_language=str(arguments.get("subtitle_language", "chinese")),
        )
        for item in received
    ]
    identities: dict[tuple[str, str], list[int]] = {}
    for index, item in enumerate(received):
        ids = item.get("ProviderIds")
        if item.get("Type") != "Movie" or not isinstance(ids, dict):
            continue
        normalized = {
            str(key).casefold(): str(value) for key, value in ids.items() if value
        }
        provider = next((key for key in ("tmdb", "imdb") if key in normalized), "")
        if provider:
            identities.setdefault((provider, normalized[provider]), []).append(index)
    duplicate_groups = 0
    for indices in identities.values():
        if len(indices) < 2:
            continue
        duplicate_groups += 1
        for index in indices:
            items[index]["issues"].append("本页疑似重复")
    counts = Counter(issue for item in items for issue in item["issues"])
    complete = start == 0 and total is not None and not has_more
    return ProviderPayload(
        summary=f"{server_label} 已检查本页 {count} 项"
        + ("（已覆盖当前全部条目）" if complete else "；仅为分页结果，不代表全库结论"),
        data={
            "server_label": server_label,
            "scope": "指定媒体库" if arguments.get("library_ref") else "所有媒体库",
            "start_index": start,
            "scanned": count,
            "reported_total": total,
            "has_more": has_more,
            "next_start_index": start + count if has_more else None,
            "complete": complete,
            "scope_stability": "实时分页，非事务快照",
            "min_resolution": int(arguments.get("min_resolution", 1080)),
            "subtitle_language": str(arguments.get("subtitle_language", "chinese")),
            "duplicate_scope": "仅本页内检查",
            "duplicate_groups": duplicate_groups,
            "issue_counts": dict(counts),
            "unknown_resolution": sum(item["resolution"] is None for item in items),
            "unknown_subtitles": sum(
                item["subtitle_state"] == "unknown" for item in items
            ),
            "playability_verified": False,
            "items": items,
        },
        source=source,
        suggestions=[
            "有下一页时按 next_start_index 继续；跨页重复未检查，未主动播放或探测 STRM 链接。"
        ],
    )
