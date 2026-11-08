"""把紧凑的剧集分季映射编译为通用光鸭文件变更操作。"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.modules.scraper import parse_release_position
from app.modules.special_media import is_special_media_name, is_special_path
from app.modules.subtitle_identity import (
    SubtitlePlanResult,
    plan_subtitle_companions,
)

_MAX_MEDIA_OPERATIONS = 200
_MAX_CREATE_DIRECTORY_OPERATIONS = 32
# 共享 special_media 已处理 S00/OVA/NCOP 等；这里只补剧集盘点中明确的
# Movie/广告/附加素材标记，不解析集号，也不按体积或篇章名称猜测。
_EXTRA_MARKER = re.compile(
    r"(?i)(?P<movie>(?<![a-z0-9])(?:movies?|films?)(?![a-z0-9])|剧场版|劇場版)"
    r"|(?P<advertisement>(?<![a-z0-9])(?:ads?|advertisements?|commercials?)(?![a-z0-9])|广告|廣告)"
    r"|(?P<extra>(?<![a-z0-9])(?:extras?|bonus|featurettes?)(?![a-z0-9])|特典|花絮)"
)


class GuangYaEpisodeNamingError(ValueError):
    """声明式剧集命名方案无法安全编译。"""


def _normalize_path(value: object, *, field: str) -> str:
    path = str(value or "").strip().replace("\\", "/")
    if not path:
        raise GuangYaEpisodeNamingError(f"{field} 不能为空")
    if not path.startswith("/"):
        path = "/" + path
    parts = [part for part in path.split("/") if part]
    if len(path) > 2048 or any(part in {".", ".."} for part in parts):
        raise GuangYaEpisodeNamingError(f"{field} 必须是精确光鸭绝对路径")
    return "/" + "/".join(parts) if parts else "/"


def _full_path(parent: str, name: str) -> str:
    return f"/{name}" if parent == "/" else f"{parent.rstrip('/')}/{name}"


def _desired_name(title: str, season: int, episode: int, entry: dict[str, Any]) -> str:
    suffix = Path(str(entry.get("name") or "")).suffix
    if not suffix:
        extension = str(entry.get("extension") or "").strip().lstrip(".")
        suffix = f".{extension}" if extension else ""
    return f"{title} - S{season:02d}E{episode:02d}{suffix}"


def _episode_details(entry: dict[str, Any], target_root: str) -> tuple[str, str, int | None, int | None]:
    """inspect 与 plan 共用同一分类，源位置只能来自共享 parser。"""
    name = str(entry.get("name") or "")
    parsed = parse_release_position(name, tv_episode_mapping_context=True)
    season, episode = parsed.get("season"), parsed.get("episode")
    parent = _normalize_path(entry.get("parent_path"), field="parent_path")
    # 只检查作品根以内的相对路径，避免上层目录名污染作品分类。
    relative_parent = parent.removeprefix(target_root)
    marker = _EXTRA_MARKER.search(name) or next(
        (match for part in Path(relative_parent).parts if (match := _EXTRA_MARKER.fullmatch(part))),
        None,
    )
    if marker:
        kind, reason = "extra", str(marker.lastgroup)
    elif episode == 0:
        kind, reason = "extra", "episode_zero"
    elif season == 0 or is_special_media_name(name) or is_special_path(relative_parent):
        kind, reason = "extra", "special_media"
    elif episode is None:
        kind, reason = "unknown", "unparsed_episode"
    else:
        kind, reason = "regular", ""
    if parsed.get("episode_end") not in (None, episode):
        return "unknown", "multi_episode_file", season, None
    return kind, reason, season, episode


def _compress_episode_numbers(values: list[int]) -> str:
    numbers = sorted(set(values))
    if not numbers:
        return ""
    chunks: list[str] = []
    start = previous = numbers[0]
    for number in numbers[1:]:
        if number == previous + 1:
            previous = number
            continue
        chunks.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = number
    chunks.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(chunks)


def _within_root(entry: dict[str, Any], target_root: str) -> bool:
    parent = _normalize_path(entry.get("parent_path"), field="parent_path")
    return parent == target_root or parent.startswith(target_root.rstrip("/") + "/")


def _plan_subtitles(
    videos: list[dict[str, Any]], subtitles: list[dict[str, Any]]
) -> SubtitlePlanResult:
    """按父目录调用共享配对器，保持同名文件不会跨目录相互匹配。"""

    if not subtitles:
        return SubtitlePlanResult(plans=[], skipped=[])

    media_by_parent: dict[str, dict[str, list[Any]]] = {}
    combined = [(item, "video") for item in videos] + [(item, "subtitle") for item in subtitles]
    for entry, kind in combined:
        parent = _normalize_path(entry.get("parent_path"), field="parent_path")
        file_id = str(entry.get("file_id") or "").strip()
        if not file_id:
            raise GuangYaEpisodeNamingError("目录观察中的媒体缺少真实file_id，无法计划字幕配对")
        media = media_by_parent.setdefault(parent, {"video": [], "subtitle": []})
        media[kind].append(
            SimpleNamespace(file_id=file_id, name=str(entry.get("name") or ""), entry=entry)
        )

    plans = []
    skipped = []
    for parent in sorted(media_by_parent):
        media = media_by_parent[parent]
        result = plan_subtitle_companions(media["video"], media["subtitle"])
        plans.extend(result.plans)
        skipped.extend(result.skipped)
    return SubtitlePlanResult(plans=plans, skipped=skipped)


def _subtitle_skips(result: SubtitlePlanResult) -> list[dict[str, str]]:
    return [
        {"name": str(item.file.name), "reason": item.reason}
        for item in result.skipped
    ]


def summarize_episode_files(
    snapshot: dict[str, Any], *, target_root: str
) -> dict[str, Any]:
    """把完整目录快照压缩为供模型判断篇章映射的小型 DTO。"""

    if snapshot.get("scan_complete") is not True:
        raise GuangYaEpisodeNamingError("光鸭目录读取不完整，请扩大扫描预算后重新读取")
    normalized_root = _normalize_path(target_root, field="target_root")
    grouped: dict[str, list[dict[str, Any]]] = {}
    entries = [item for item in snapshot.get("items") or () if isinstance(item, dict)]
    videos = [
        entry for entry in entries
        if not bool(entry.get("is_dir"))
        and str(entry.get("media_kind") or "") == "video"
        and _within_root(entry, normalized_root)
    ]
    subtitles = [
        entry for entry in entries
        if not bool(entry.get("is_dir"))
        and str(entry.get("media_kind") or "") == "subtitle"
        and _within_root(entry, normalized_root)
    ]
    for entry in videos:
        parent_path = _normalize_path(entry.get("parent_path"), field="parent_path")
        grouped.setdefault(parent_path, []).append(entry)

    subtitle_plan = _plan_subtitles(videos, subtitles)

    summaries: list[dict[str, Any]] = []
    counts = {"regular": 0, "extra": 0, "unknown": 0}
    total_unparsed = 0
    for parent_path, items in sorted(
        grouped.items(), key=lambda pair: (pair[0] != normalized_root, pair[0].casefold())
    ):
        buckets: dict[tuple[str, str], list[tuple[dict[str, Any], int | None, int | None]]] = {}
        for entry in sorted(items, key=lambda item: str(item.get("name") or "").casefold()):
            kind, reason, season, episode = _episode_details(entry, normalized_root)
            counts[kind] += 1
            buckets.setdefault((kind, reason), []).append((entry, season, episode))
        regular = _summarize_positions(buckets.get(("regular", ""), []))
        exceptions: dict[str, list[dict[str, Any]]] = {
            kind: [
                {"reason": reason, **_summarize_positions(rows)}
                for (category, reason), rows in sorted(buckets.items()) if category == kind
            ]
            for kind in ("extra", "unknown")
        }
        unparsed = sum(episode is None for rows in buckets.values() for _, _, episode in rows)
        total_unparsed += unparsed
        summaries.append({
            "source_path": parent_path,
            "file_ids": [item["file_id"] for item in items],
            "directory_name": "(共同父目录)" if parent_path == normalized_root else Path(parent_path).name,
            **regular,
            "video_count": len(items),
            "small_video_count": sum(row["small_video_count"] for row in [regular, *exceptions["extra"], *exceptions["unknown"]]),
            "parsed_count": len(items) - unparsed,
            "unparsed_count": unparsed,
            "regular_count": regular["video_count"],
            "extra_count": sum(row["video_count"] for row in exceptions["extra"]),
            "unknown_count": sum(row["video_count"] for row in exceptions["unknown"]),
            "extras": exceptions["extra"],
            "unknown": exceptions["unknown"],
        })
    if not summaries:
        raise GuangYaEpisodeNamingError("目标目录中没有可用于剧集命名盘点的视频")
    return {
        "target_root": normalized_root,
        "video_count": sum(counts.values()),
        "subtitle_count": len(subtitles),
        "matched_subtitle_count": len(subtitle_plan.plans),
        "unmatched_subtitle_count": len(subtitle_plan.skipped),
        "subtitle_skips": _subtitle_skips(subtitle_plan),
        "source_group_count": len(summaries),
        "unparsed_count": total_unparsed,
        **{f"{kind}_count": count for kind, count in counts.items()},
        "mapping_status": "needs_verification",
        "mapping_note": "positions 仅为正片候选的源文件集号，不是 TMDB 映射；缺少可靠依据时请说明待核对，不得按目录或已观察集数推断偏移。extra 默认排除，unknown 不自动分配集号。",
        "groups": summaries,
    }


def _summarize_positions(rows: list[tuple[dict[str, Any], int | None, int | None]]) -> dict[str, Any]:
    positions: dict[int | None, list[int]] = {}
    for _, season, episode in rows:
        if episode is not None:
            positions.setdefault(season, []).append(episode)
    return {
        "video_count": len(rows),
        "small_video_count": sum(
            isinstance(entry.get("size"), int) and 0 <= entry["size"] < 5 * 1024 * 1024
            for entry, _, _ in rows
        ),
        "positions": [
            {"source_season": season, "episodes": _compress_episode_numbers(episodes), "count": len(set(episodes))}
            for season, episodes in sorted(positions.items(), key=lambda pair: -1 if pair[0] is None else pair[0])
        ],
        # 按类别取首/中/尾样例，异常不会被目录中的前三个正片挤掉；计数覆盖全部。
        "samples": [str(rows[i][0].get("name") or "") for i in sorted({0, len(rows) // 2, len(rows) - 1})] if rows else [],
        "samples_truncated": len(rows) > 3,
    }


def compile_episode_naming_operations(
    snapshot: dict[str, Any],
    *,
    title: str,
    target_root: str,
    groups: list[dict[str, Any]],
) -> dict[str, Any]:
    """按显式篇章映射展开视频与唯一配套字幕的文件变更。"""

    if snapshot.get("scan_complete") is not True:
        raise GuangYaEpisodeNamingError("光鸭目录读取不完整，请扩大扫描预算后重新读取")
    normalized_title = (title or "").strip()
    if not 1 <= len(normalized_title) <= 180:
        raise GuangYaEpisodeNamingError("title 长度必须在 1 到 180 之间")
    normalized_root = _normalize_path(target_root, field="target_root")
    if not isinstance(groups, list) or not 1 <= len(groups) <= 32:
        raise GuangYaEpisodeNamingError("groups 必须包含 1 到 32 个篇章映射")

    entries = [item for item in snapshot.get("items") or () if isinstance(item, dict)]
    videos = [
        item for item in entries
        if not bool(item.get("is_dir"))
        and str(item.get("media_kind") or "") == "video"
        and _within_root(item, normalized_root)
    ]
    subtitles = [
        item for item in entries
        if not bool(item.get("is_dir"))
        and str(item.get("media_kind") or "") == "subtitle"
        and _within_root(item, normalized_root)
    ]
    if any(not str(item.get("file_id") or "").strip() for item in [*videos, *subtitles]):
        raise GuangYaEpisodeNamingError("目录观察中的媒体缺少真实file_id，拒绝编译")

    existing_directories = {
        _normalize_path(item.get("parent_path"), field="parent_path")
        for item in entries
        if str(item.get("parent_path") or "").strip()
    }
    existing_directories.update(
        _full_path(
            _normalize_path(item.get("parent_path"), field="parent_path"),
            str(item.get("name") or ""),
        )
        for item in entries
        if bool(item.get("is_dir")) and str(item.get("name") or "").strip()
    )

    selected_ids: set[str] = set()
    selected_videos: list[dict[str, Any]] = []
    group_states: list[dict[str, Any]] = []
    for index, group in enumerate(groups, start=1):
        if not isinstance(group, dict):
            raise GuangYaEpisodeNamingError(f"第 {index} 个篇章映射格式无效")
        if type(group.get("include_extras", False)) is not bool:
            raise GuangYaEpisodeNamingError("include_extras 必须是布尔值")
        source_path = _normalize_path(group.get("source_path"), field="source_path")
        if not (source_path == normalized_root or source_path.startswith(normalized_root.rstrip("/") + "/")):
            raise GuangYaEpisodeNamingError("篇章来源不在当前真实作品根内")
        target_season = int(group["target_season"])
        source_start = int(group["source_episode_start"])
        source_end = int(group["source_episode_end"])
        target_start = int(group.get("target_episode_start", 1))
        if source_start < 0 or source_end < source_start:
            raise GuangYaEpisodeNamingError("源起始集号不能小于 0，结束值不能小于起始值")
        if target_season < 0 or target_start < 1:
            raise GuangYaEpisodeNamingError("目标季号不能小于 0，目标集号必须从 1 开始")
        source_season = group.get("source_season")
        if source_season is not None:
            source_season = int(source_season)
        include_extras = group.get("include_extras", source_season == 0 or target_season == 0)
        name_contains = str(group.get("name_contains") or "").strip()
        expected_count = group.get("expected_count")
        if expected_count is not None:
            expected_count = int(expected_count)

        episode_entries: list[tuple[int, dict[str, Any]]] = []
        unparsed = included_extras = excluded_extras = 0
        for entry in videos:
            if _normalize_path(entry.get("parent_path"), field="parent_path") != source_path:
                continue
            if name_contains and name_contains.casefold() not in str(entry.get("name") or "").casefold():
                continue
            kind, _, parsed_season, parsed_episode = _episode_details(entry, normalized_root)
            if kind == "unknown" or parsed_episode is None:
                unparsed += 1
                continue
            if source_season is not None and parsed_season != source_season:
                continue
            if not source_start <= parsed_episode <= source_end:
                continue
            if kind == "extra" and not include_extras:
                excluded_extras += 1
                continue
            included_extras += kind == "extra"
            episode_entries.append((parsed_episode, entry))

        episode_entries.sort(key=lambda pair: (pair[0], str(pair[1].get("name") or "").casefold()))
        if not episode_entries:
            detail = "，且存在无法识别集号的文件" if unparsed else ""
            raise GuangYaEpisodeNamingError(
                f"第 {index} 个篇章映射没有匹配到可处理文件（非正片默认排除）{detail}"
            )
        if expected_count is not None and len(episode_entries) != expected_count:
            raise GuangYaEpisodeNamingError(
                f"第 {index} 个篇章映射预期 {expected_count} 集，实际匹配 {len(episode_entries)} 集"
            )

        rows = []
        for source_episode, entry in episode_entries:
            file_id = str(entry.get("file_id") or "").strip()
            if not file_id or file_id in selected_ids:
                raise GuangYaEpisodeNamingError("篇章映射包含重复或无效对象")
            selected_ids.add(file_id)
            row = {"file_id": file_id, "source_episode": source_episode, "entry": entry}
            rows.append(row)
            selected_videos.append(entry)
        group_states.append({
            "index": index,
            "source_path": source_path,
            "target_season": target_season,
            "source_start": source_start,
            "target_start": target_start,
            "selected": rows,
            "unparsed": unparsed,
            "included_extras": included_extras,
            "excluded_extras": excluded_extras,
        })

    all_subtitle_plan = _plan_subtitles(videos, subtitles)
    selected_subtitle_plan = _plan_subtitles(selected_videos, subtitles)
    selected_candidate_ids = {
        id(plan.file.entry) for plan in selected_subtitle_plan.plans
    }
    selected_candidate_ids.update(
        id(skip.file.entry)
        for skip in selected_subtitle_plan.skipped
        if skip.reason_code in {"ambiguous-video", "duplicate-target"}
    )
    for skip in all_subtitle_plan.skipped:
        if (
            skip.reason_code in {"ambiguous-video", "duplicate-target"}
            and id(skip.file.entry) in selected_candidate_ids
        ):
            raise GuangYaEpisodeNamingError(
                f"所选视频存在字幕歧义，拒绝编译：{skip.file.name}（{skip.reason}）"
            )

    selected_subtitles = [
        plan for plan in all_subtitle_plan.plans
        if plan.video_file_id in selected_ids
    ]
    unselected_subtitles = [
        plan for plan in all_subtitle_plan.plans if plan.video_file_id not in selected_ids
    ]
    selected_video_ids = set(selected_ids)
    subtitle_ids = [
        str(plan.file.entry.get("file_id") or "").strip()
        for plan in selected_subtitles
    ]
    if any(not file_id for file_id in subtitle_ids) or len(set(subtitle_ids)) != len(subtitle_ids):
        raise GuangYaEpisodeNamingError("字幕计划包含重复或无效对象 file_id")
    if selected_video_ids.intersection(subtitle_ids):
        raise GuangYaEpisodeNamingError("字幕计划重复引用了已选视频对象 file_id")

    target_names: set[tuple[str, str]] = set()
    video_targets: dict[str, dict[str, Any]] = {}
    create_paths: set[str] = set()
    create_operations: list[dict[str, Any]] = []
    change_operations: list[dict[str, Any]] = []
    group_summaries: list[dict[str, Any]] = []
    selected_ids.update(subtitle_ids)
    skipped_noop = 0

    for state in group_states:
        index = state["index"]
        selected = state["selected"]
        episodes: set[int] = set()
        target_season = state["target_season"]
        target_path = _full_path(normalized_root, f"Season {target_season:02d}")
        rename_items: list[dict[str, Any]] = []
        relocate_items: list[dict[str, Any]] = []
        for row in selected:
            source_episode = row["source_episode"]
            if source_episode in episodes:
                raise GuangYaEpisodeNamingError(
                    f"第 {index} 个篇章映射存在重复源集号 E{source_episode:02d}"
                )
            episodes.add(source_episode)
            target_episode = state["target_start"] + source_episode - state["source_start"]
            if not 1 <= target_episode <= 9999:
                raise GuangYaEpisodeNamingError("映射后的目标集号超出 1 到 9999")
            entry = row["entry"]
            desired = _desired_name(normalized_title, target_season, target_episode, entry)
            target_key = (target_path.casefold(), desired.casefold())
            if target_key in target_names:
                raise GuangYaEpisodeNamingError(
                    f"多个篇章映射会生成同一目标：S{target_season:02d}E{target_episode:02d}"
                )
            target_names.add(target_key)
            video_targets[row["file_id"]] = {
                "target_path": target_path,
                "target_episode": target_episode,
                "desired_name": desired,
            }
            current_parent = _normalize_path(entry.get("parent_path"), field="parent_path")
            current_name = str(entry.get("name") or "")
            if current_parent == target_path:
                if current_name == desired:
                    skipped_noop += 1
                else:
                    rename_items.append(
                        {"op": "rename", "file_id": row["file_id"], "new_name": desired}
                    )
            else:
                relocate_items.append({"op": "relocate", "file_id": row["file_id"], "target_path": target_path, "new_name": desired})

        if (relocate_items or rename_items) and target_path not in existing_directories and target_path not in create_paths:
            create_paths.add(target_path)
            create_operations.append(
                {"op": "create_directory", "parent_path": normalized_root, "name": f"Season {target_season:02d}"}
            )
        change_operations.extend(rename_items)
        change_operations.extend(relocate_items)
        group_summaries.append({
            "source_directory": "(共同父目录)" if state["source_path"] == normalized_root else Path(state["source_path"]).name,
            "season": target_season,
            "matched": len(selected),
            "included_extra_count": state["included_extras"],
            "excluded_extra_count": state["excluded_extras"],
            "unknown_count": state["unparsed"],
            "renamed_in_place": len(rename_items),
            "relocated": len(relocate_items),
            "source_episode_start": selected[0]["source_episode"],
            "source_episode_end": selected[-1]["source_episode"],
            "target_episode_start": state["target_start"],
            "target_episode_end": state["target_start"] + selected[-1]["source_episode"] - state["source_start"],
        })

    for plan in selected_subtitles:
        video_id = plan.video_file_id
        target = video_targets[video_id]
        entry = plan.file.entry
        file_id = str(entry.get("file_id") or "").strip()
        desired = plan.target_name(target["desired_name"])
        target_key = (target["target_path"].casefold(), desired.casefold())
        if target_key in target_names:
            raise GuangYaEpisodeNamingError(f"字幕目标名称冲突：{desired}")
        target_names.add(target_key)
        current_parent = _normalize_path(entry.get("parent_path"), field="parent_path")
        current_name = str(entry.get("name") or "")
        if current_parent == target["target_path"]:
            if current_name == desired:
                skipped_noop += 1
            else:
                change_operations.append({"op": "rename", "file_id": file_id, "new_name": desired})
        else:
            change_operations.append({
                "op": "relocate",
                "file_id": file_id,
                "target_path": target["target_path"],
                "new_name": desired,
            })

    directory_ids = {_full_path(item["parent_path"], item["name"]): item["file_id"] for item in entries if item.get("is_dir")}
    directory_ids[normalized_root] = snapshot.get("root", {}).get("file_id")
    for operation in create_operations:
        if directory_ids.get(operation["parent_path"]):
            operation["parent_id"] = directory_ids[operation["parent_path"]]
    by_id = {entry["file_id"]: entry for entry in entries}
    for operation in change_operations:
        entry = by_id[operation["file_id"]]
        if operation.get("target_path") in directory_ids:
            operation["target_id"] = directory_ids[operation["target_path"]]
        operation["expected"] = {key: entry[key] for key in ("file_id", "parent_id", "name", "is_dir", "size", "etag", "updated_at")}
        operation["expected"]["path"] = _full_path(entry["parent_path"], entry["name"])
    operations = [*create_operations, *change_operations]
    selected_file_count = len(selected_ids)
    if selected_file_count > _MAX_MEDIA_OPERATIONS:
        raise GuangYaEpisodeNamingError(
            f"完整方案包含 {selected_file_count} 个媒体文件，单个冻结计划最多 "
            f"{_MAX_MEDIA_OPERATIONS} 个；请按完整季拆分 groups，不能截断同一季"
        )
    if len(create_operations) > _MAX_CREATE_DIRECTORY_OPERATIONS:
        raise GuangYaEpisodeNamingError(
            f"完整方案需要创建 {len(create_operations)} 个目录，单个冻结计划最多 "
            f"{_MAX_CREATE_DIRECTORY_OPERATIONS} 个"
        )
    effective_total = len(operations)
    if effective_total == 0:
        raise GuangYaEpisodeNamingError("所选文件已经符合目标分季命名，无需变更")

    return {
        "operations": operations,
        "effective_total": effective_total,
        "selected_files": selected_file_count,
        "video_count": sum(len(state["selected"]) for state in group_states),
        "subtitle_count": len(selected_subtitles),
        "unmatched_subtitle_count": len(all_subtitle_plan.skipped),
        "unselected_subtitle_count": len(unselected_subtitles),
        "subtitle_skips": [*_subtitle_skips(all_subtitle_plan), *(
            {"name": str(plan.file.name), "reason": "对应视频未被本次篇章映射选中，字幕未处理"}
            for plan in unselected_subtitles
        )],
        "included_extra_count": sum(group["included_extras"] for group in group_states),
        "created_directories": len(create_operations),
        "skipped_noop": skipped_noop,
        "groups": group_summaries,
    }


def episode_inventory(client, file_id: str, *, should_stop=None) -> dict:
    """完整真实目录事实与分类；源集号不能证明TMDB映射，缺依据时仍需核对。"""
    from app.modules.guangya_workspace import list_files
    if file_id == "0":
        raise GuangYaEpisodeNamingError("剧集命名必须指定非账号根目录的真实ID")
    snapshot = list_files(client, file_id, recursive=True, should_stop=should_stop)
    root = snapshot["root"]
    return {**snapshot, **summarize_episode_files(snapshot, target_root=root["path"])}


def preview_episode_names(client, file_id: str, *, title: str, groups: list[dict], should_stop=None) -> dict:
    """按明确source_path和季集映射编译真实ID操作；无观察句柄、TTL或单独执行入口。"""
    if not isinstance(title, str):
        raise GuangYaEpisodeNamingError("title必须是字符串")
    if not isinstance(groups, list) or not 1 <= len(groups) <= 32:
        raise GuangYaEpisodeNamingError("groups必须包含1到32个篇章映射")
    allowed = {"source_path", "target_season", "source_episode_start", "source_episode_end", "target_episode_start",
               "source_season", "name_contains", "expected_count", "include_extras"}
    for group in groups:
        if not isinstance(group, dict) or set(group) - allowed:
            raise GuangYaEpisodeNamingError("篇章映射含不支持字段；使用真实source_path，不使用来源序号")
        for key in ("source_path", "target_season", "source_episode_start", "source_episode_end", "expected_count"):
            if key not in group:
                raise GuangYaEpisodeNamingError("篇章映射缺少" + key)
        if not isinstance(group["source_path"], str):
            raise GuangYaEpisodeNamingError("source_path必须为实际读取的路径")
        for key, minimum, maximum in (("target_season", 0, 999), ("source_season", 0, 999),
                ("source_episode_start", 0, 9999), ("source_episode_end", 0, 9999),
                ("target_episode_start", 1, 9999), ("expected_count", 1, 200)):
            value = group.get(key)
            if value is None and key in {"target_season", "source_episode_start", "source_episode_end", "expected_count"}:
                raise GuangYaEpisodeNamingError(key + "不能为空")
            if value is not None and (type(value) is not int or not minimum <= value <= maximum):
                raise GuangYaEpisodeNamingError(key + "必须是范围内整数")
        if type(group.get("include_extras", False)) is not bool:
            raise GuangYaEpisodeNamingError("include_extras必须为布尔值")
        if "name_contains" in group and (not isinstance(group["name_contains"], str) or len(group["name_contains"]) > 160):
            raise GuangYaEpisodeNamingError("name_contains必须是最多160字符的字面文本")
    snapshot = episode_inventory(client, file_id, should_stop=should_stop)
    compiled = compile_episode_naming_operations(snapshot, title=title, target_root=snapshot["target_root"], groups=groups)
    return {**compiled, "file_id": file_id, "target_root": snapshot["target_root"], "cloud_write": False}
