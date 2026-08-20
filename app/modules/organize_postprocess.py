"""整理阶段的纯后处理规则。

只处理已存在的规划/文件快照，不执行数据库写入、远程 RPC 或文件系统操作。
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


SUBTITLE_EXTS = frozenset({"srt", "ass", "ssa", "sup", "vtt", "sub", "idx"})


def normalized_stem(name: str) -> str:
    stem = name.rsplit(".", 1)[0].lower()
    stem = re.sub(
        r"(?i)([._ -](?:zh|chs|cht|eng|en|cn|sc|tc|forced|default))+$",
        "",
        stem,
    )
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", stem)


def companion_target_name(
    video_original: str,
    video_target: str,
    companion_name: str,
) -> str:
    """将明确属于视频的伴随文件改为相同 basename，并保留语义尾缀。"""
    original_base = video_original.rsplit(".", 1)[0]
    target_base = video_target.rsplit(".", 1)[0]
    if not companion_name.lower().startswith(original_base.lower()):
        return companion_name
    suffix = companion_name[len(original_base):]
    return f"{target_base}{suffix}" if suffix else companion_name


def media_role(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in SUBTITLE_EXTS:
        return "subtitle"
    if ext == "nfo":
        return "nfo"
    if ext in {"jpg", "jpeg", "png", "webp"}:
        return "image"
    return "metadata"


def replacement_delete_block_reason(
    *,
    expected_old: Any,
    expected_new: Any,
    old_detail: Any | None,
    new_detail: Any | None,
    target_files: Sequence[Any],
    scan_errors: Sequence[str],
    move_succeeded: bool,
) -> str:
    """验证替换删除的已读取快照；失败时返回稳定的阻断原因。"""
    if not move_succeeded:
        return "移动失败，禁止将旧文件移入回收站"
    if scan_errors:
        return "扫描错误，禁止将旧文件移入回收站"
    if old_detail is None or new_detail is None:
        return "文件详情不可读，禁止将旧文件移入回收站"
    if not any(item.file_id == expected_new.file_id for item in target_files):
        return "替换文件缺失，禁止将旧文件移入回收站"
    if not any(item.file_id == expected_old.file_id for item in target_files):
        return "旧文件备份缺失，禁止将旧文件移入回收站"
    if old_detail.file_id != expected_old.file_id or new_detail.file_id != expected_new.file_id:
        return "文件详情身份不一致，禁止将旧文件移入回收站"
    if not expected_old.etag or int(expected_old.size or 0) <= 0:
        return "旧文件 GCID/size 歧义，禁止将旧文件移入回收站"
    if old_detail.etag != expected_old.etag or int(old_detail.size or 0) != int(expected_old.size or 0):
        return "旧文件 GCID/size 与快照不一致，禁止将旧文件移入回收站"
    old_target = next(
        (item for item in target_files if item.file_id == expected_old.file_id),
        None,
    )
    if old_target is None:
        return "旧文件备份缺失，禁止将旧文件移入回收站"
    if (
        old_target.etag != expected_old.etag
        or int(old_target.size or 0) != int(expected_old.size or 0)
    ):
        return "旧文件 GCID/size 与目录快照不一致，禁止将旧文件移入回收站"
    if expected_new.etag and new_detail.etag != expected_new.etag:
        return "替换文件 GCID 不一致，禁止将旧文件移入回收站"
    if int(expected_new.size or 0) > 0 and int(new_detail.size or 0) != int(expected_new.size or 0):
        return "替换文件 size 不一致，禁止将旧文件移入回收站"
    return ""


def normalize_media_number(value: object) -> int | None:
    """把外部解析器的季集值收敛为 SQLite 可绑定标量。"""
    candidates = value if isinstance(value, (list, tuple, set)) else (value,)
    for candidate in candidates:
        if candidate in (None, "") or isinstance(candidate, bool):
            continue
        try:
            number = int(float(candidate))
        except (TypeError, ValueError, OverflowError):
            continue
        if number >= 0:
            return number
    return None


def resolved_plan_position(
    plan: Any,
    parsed: Mapping[str, Any],
) -> tuple[int | None, int | None]:
    """优先采用规划阶段最终季集号，原始文件解析只作兼容回退。"""
    season = plan.season if plan.season is not None else parsed.get("season")
    episode = plan.episode if plan.episode is not None else parsed.get("episode")
    return season, episode


def media_notification_item(
    plan: Any,
    actual_name: str,
    parsed: Mapping[str, Any],
    *,
    season_present_episodes: list[int] | None = None,
    resolved_position: tuple[int | None, int | None] | None = None,
) -> dict[str, Any]:
    """生成通知层所需的最小媒体数据，不耦合 Telegram 渲染。"""
    match = getattr(plan, "match", None)
    season, episode = resolved_position or resolved_plan_position(plan, parsed)
    item: dict[str, Any] = {
        "directory": plan.original_path or "/",
        "title": getattr(match, "title", "") if match is not None else "",
        "year": (getattr(match, "year", "") if match is not None else "") or plan.year,
        "media_type": getattr(match, "media_type", "") if match is not None else "",
        "tmdb_id": getattr(match, "tmdb_id", "") if match is not None else "",
        "season": season,
        "episode": episode,
        "season_total": plan.season_total,
        "source": "光鸭云盘",
        "category": plan.target_path,
        "filename": actual_name,
        "size": plan.size,
        "backdrop_path": plan.backdrop_path,
        "poster_path": plan.poster_path,
    }
    if season_present_episodes is not None:
        item["season_present_episodes"] = season_present_episodes
    return item
