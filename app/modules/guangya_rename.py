"""光鸭名称转换与冲突检查；只生成真实ID操作，由公共cloud.change唯一执行。"""
from __future__ import annotations

import re
from collections import Counter, deque
from typing import Any
from app.clients.guangya import GuangYaClient, GuangYaFile

_MAX_TARGETS = 32
_MAX_RENAMES = 10_000
_MAX_SCANNED_ITEMS = 100_000
_MAX_SCANNED_DIRS = 5_000
_MAX_NAME_LENGTH = 255
CANONICAL_RENAME_MODES = frozenset({"remove_bitrate", "replace_text", "media_hygiene"})
_COMPACT_BITRATE_RE = re.compile(
    r"\.(?!(?:26[0-9])\.)(?:\d+(?:\.\d+)?)\s*Mbps(?=\.|$)",
    re.IGNORECASE,
)
_DASH_BITRATE_RE = re.compile(
    r"\s+-\s+(?:\d+(?:\.\d+)?)\s*Mbps(?=\.|[-_\s]|$)",
    re.IGNORECASE,
)
_SPACED_BITRATE_RE = re.compile(
    r"\s+(?:\d+(?:\.\d+)?)\s*Mbps(?=\.|[-_\s]|$)",
    re.IGNORECASE,
)


class GuangYaNamingError(ValueError):
    """名称编译或目录范围无效；尚未提交写操作。"""


def _normalize_path(value: object) -> str:
    path = str(value or "").strip().replace("\\", "/")
    if not path.startswith("/") or len(path) > 2048:
        raise GuangYaNamingError("光鸭路径必须是绝对路径")
    parts = [part for part in path.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise GuangYaNamingError("不能重命名光鸭根目录")
    if any(len(part) > _MAX_NAME_LENGTH for part in parts):
        raise GuangYaNamingError("光鸭路径组件过长")
    return "/" + "/".join(parts)

def _validate_name(value: object) -> str:
    name = str(value or "")
    if not name or name != name.strip() or len(name) > _MAX_NAME_LENGTH:
        raise GuangYaNamingError("目标名称为空、过长或包含首尾空格")
    if name in {".", ".."} or any(char in name for char in ("/", "\\", "\0")):
        raise GuangYaNamingError("目标名称包含不允许的字符")
    if any(ord(char) < 32 for char in name):
        raise GuangYaNamingError("目标名称包含控制字符")
    return name

def remove_legacy_bitrate(name: str) -> str:
    """删除紧凑或旧式空格 Mbps 字段，不误伤 H.264/H.265。"""
    # 先处理带空格的完整字段，避免紧凑规则把 ``2.4 Mbps`` 的
    # 小数部分误当成独立的 ``.4Mbps``。
    updated = _DASH_BITRATE_RE.sub("", (name or ""))
    updated = _SPACED_BITRATE_RE.sub("", updated)
    updated = _COMPACT_BITRATE_RE.sub("", updated)
    return updated

def _extension(name: str) -> str:
    match = re.search(r"[.。．]([A-Za-z0-9]{1,10})$", (name or ""))
    return match.group(1).casefold() if match else ""

def _item_extension(item: GuangYaFile) -> str:
    declared = (item.extension or "").strip().lstrip(".").casefold()
    return declared or _extension(item.name)

def _transformed_name(
    item: GuangYaFile,
    *,
    mode: str,
    find_text: str = "",
    replace_text: str = "",
) -> str:
    if mode == "remove_bitrate":
        candidate = _validate_name(remove_legacy_bitrate(item.name))
    elif mode == "replace_text":
        if find_text not in item.name:
            return item.name
        candidate = _validate_name(item.name.replace(find_text, replace_text))
    else:
        raise GuangYaNamingError("不支持的重命名方式")
    if not item.is_dir and _extension(candidate) != _item_extension(item):
        raise GuangYaNamingError("批量重命名不允许修改文件扩展名")
    return candidate

def _cached_list_dir(
    client: GuangYaClient,
    cache: dict[str, list[GuangYaFile]],
    parent_id: str,
) -> list[GuangYaFile]:
    key = parent_id
    if key not in cache:
        cache[key] = client.list_dir(key)
    return cache[key]


def _entry(item: GuangYaFile, *, parent_path: str, new_name: str) -> dict[str, Any]:
    old_path = (
        (parent_path.rstrip("/") + "/" + item.name)
        if parent_path != "/"
        else "/" + item.name
    )
    new_path = (
        (parent_path.rstrip("/") + "/" + new_name)
        if parent_path != "/"
        else "/" + new_name
    )
    return {
        "file_id": item.file_id,
        "parent_id": item.parent_id,
        "old_name": item.name,
        "new_name": new_name,
        "old_path": old_path,
        "new_path": new_path,
        "is_dir": item.is_dir,
        "size": max(0, (item.size or 0)),
        "etag": (item.etag or ""),
        "extension": (item.extension or _extension(item.name)).casefold(),
        "updated_at": max(0, item.updated_at or 0),
    }

def _finalize_rename_plan(
    client: GuangYaClient,
    *,
    mode: str,
    recursive: bool,
    targets: list[str],
    limit: int,
    cache: dict[str, list[GuangYaFile]],
    raw_entries: list[dict[str, Any]],
    scanned_items: int,
    scanned_dirs: int,
    no_change: int,
    transform: dict[str, str] | None = None,
    extra_stats: dict[str, int] | None = None,
) -> dict[str, Any]:
    by_target: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for entry in raw_entries:
        by_target.setdefault(
            (entry["parent_id"], entry["new_name"].casefold()),
            [],
        ).append(entry)

    conflicts: list[dict[str, Any]] = []
    safe_entries: list[dict[str, Any]] = []
    selected_ids = {str(entry["file_id"]) for entry in raw_entries}
    for group in by_target.values():
        first = group[0]
        reason = ""
        if len(group) > 1:
            reason = "多个文件会映射到同一目标名称"
        else:
            siblings = _cached_list_dir(
                client,
                cache,
                str(first["parent_id"]),
            )
            occupied = [
                sibling
                for sibling in siblings
                if sibling.name.casefold() == first["new_name"].casefold()
                and sibling.file_id != first["file_id"]
            ]
            if occupied:
                reason = (
                    "目标名称已存在"
                    if occupied[0].file_id not in selected_ids
                    else "目标名称与同批次其他文件冲突"
                )
        if reason:
            conflicts.extend({**entry, "reason": reason} for entry in group)
        else:
            safe_entries.extend(group)

    summary = (
        f"找到 {len(safe_entries)} 个可安全重命名对象"
        if safe_entries
        else "没有可安全执行的名称变更"
    )
    extension_counts = Counter(
        entry["extension"] or "directory" for entry in safe_entries
    )
    stats = {
        "scanned_items": max(0, scanned_items),
        "scanned_dirs": max(0, scanned_dirs),
        "matched": len(raw_entries),
        "rename_count": len(safe_entries),
        "conflict_count": len(conflicts),
        "no_change_count": max(0, no_change),
    }
    for key, value in (extra_stats or {}).items():
        stats[key] = max(0, (value or 0))
    entries = sorted(safe_entries, key=lambda entry: (entry["old_path"].casefold(), entry["file_id"]))
    return {"status": "ready" if entries else "no_changes", "mode": mode, "targets": list(targets),
            "recursive": recursive, "summary": summary, "stats": stats, "extension_counts": dict(sorted(extension_counts.items())),
            "entries": entries, "conflicts": sorted(conflicts, key=lambda entry: (entry["old_path"].casefold(), entry["file_id"])),
            "transform": dict(transform or {}),
            "operations": [{"op": "rename", "file_id": item["file_id"], "new_name": item["new_name"],
                            "expected": {"file_id": item["file_id"], "parent_id": item["parent_id"], "name": item["old_name"],
                                         "is_dir": item["is_dir"], "size": item["size"], "etag": item["etag"], "updated_at": item["updated_at"]}}
                           for item in entries]}


def preview_explicit_names(
    client: GuangYaClient,
    *,
    target: str,
    changes: list[tuple[GuangYaFile, str, str]],
    cache: dict[str, list[GuangYaFile]],
    scanned_items: int,
    scanned_dirs: int,
    no_change: int = 0,
    limit: int = 1_000,
    mode: str = "media_hygiene",
    extra_stats: dict[str, int] | None = None,
    transform: dict[str, str] | None = None,
) -> dict[str, Any]:
    """把内部扫描器产生的精确名称映射冻结为通用重命名计划。"""
    normalized_target = _normalize_path(target)
    safe_mode = (mode or "").strip().casefold()
    if safe_mode != "media_hygiene":
        raise GuangYaNamingError("显式重命名计划类型无效")
    safe_limit = max(1, min(limit, _MAX_RENAMES))
    if len(changes) > safe_limit:
        raise GuangYaNamingError(
            f"匹配文件超过本次上限 {safe_limit} 个，请缩小范围或提高 limit"
        )
    raw_entries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item, parent_path, proposed_name in changes:
        file_id = (item.file_id or "").strip()
        if not file_id or file_id in seen_ids:
            raise GuangYaNamingError("显式重命名计划包含重复或无效对象")
        candidate = _validate_name(proposed_name)
        if not item.is_dir and _extension(candidate) != _item_extension(item):
            raise GuangYaNamingError("媒体名称清理不允许修改文件扩展名")
        if candidate == item.name:
            continue
        seen_ids.add(file_id)
        raw_entries.append(_entry(item, parent_path=parent_path, new_name=candidate))
    return _finalize_rename_plan(
        client,
        mode=safe_mode,
        recursive=True,
        targets=[normalized_target],
        limit=safe_limit,
        cache=cache,
        raw_entries=raw_entries,
        scanned_items=scanned_items,
        scanned_dirs=scanned_dirs,
        no_change=no_change,
        transform={"strategy": "nsfw_media_hygiene", **dict(transform or {})},
        extra_stats=extra_stats,
    )

def preview_rename(
    client: GuangYaClient,
    *,
    file_ids: list[str],
    mode: str,
    recursive: bool = False,
    limit: int = 10_000,
    find_text: str = "",
    replace_text: str = "",
) -> dict[str, Any]:
    """按真实ID完整扫描并编译安全名称操作；不保存计划或执行写入。"""
    from app.modules.guangya_workspace import file_location
    if not isinstance(file_ids, list) or not file_ids or any(not isinstance(item, str) or not item or item == "0" for item in file_ids):
        raise GuangYaNamingError("必须指定真实的非根文件或目录ID数组")
    locations = [file_location(client, item) for item in dict.fromkeys(file_ids)]
    if len(locations) > _MAX_TARGETS:
        raise GuangYaNamingError(f"一次只能指定1到{_MAX_TARGETS}个范围")
    if type(recursive) is not bool or type(limit) is not int or not 1 <= limit <= _MAX_RENAMES:
        raise GuangYaNamingError("recursive或完整名称读取预算无效")
    normalized_targets = [item["path"] for item in locations]
    safe_mode = (mode or "").strip().casefold()
    if safe_mode not in {"remove_bitrate", "replace_text"}:
        raise GuangYaNamingError("重命名方式无效")
    safe_limit = max(1, min(limit, _MAX_RENAMES))
    if safe_mode == "replace_text":
        if not find_text or len(find_text) > 120 or len(replace_text) > 120:
            raise GuangYaNamingError("文本替换参数无效")
        if find_text == replace_text:
            raise GuangYaNamingError("替换前后文本相同")

    cache: dict[str, list[GuangYaFile]] = {}
    candidates: dict[str, dict[str, Any]] = {}
    no_change = 0
    scanned_items = 0
    scanned_dirs = 0

    def consider(item: GuangYaFile, parent_path: str) -> None:
        nonlocal no_change
        candidate = _transformed_name(
            item,
            mode=safe_mode,
            find_text=find_text,
            replace_text=replace_text,
        )
        if candidate == item.name:
            no_change += 1
            return
        candidates.setdefault(
            item.file_id,
            _entry(item, parent_path=parent_path, new_name=candidate),
        )
        if len(candidates) > safe_limit:
            raise GuangYaNamingError(
                f"匹配文件超过本次上限 {safe_limit} 个，请缩小范围或提高 limit"
            )

    for location in locations:
        target, parent_path = location["path"], location["parent_path"]
        item = GuangYaFile(**{key: location[key] for key in GuangYaFile.__dataclass_fields__})
        if not item.is_dir:
            consider(item, parent_path)
            continue
        queue = deque([(item.file_id, target)])
        seen_dirs: set[str] = set()
        while queue:
            directory_id, directory_path = queue.popleft()
            if directory_id in seen_dirs:
                continue
            seen_dirs.add(directory_id)
            scanned_dirs += 1
            if scanned_dirs > _MAX_SCANNED_DIRS:
                raise GuangYaNamingError("递归目录数量超过安全上限")
            items = _cached_list_dir(client, cache, directory_id)
            for child in items:
                scanned_items += 1
                if scanned_items > _MAX_SCANNED_ITEMS:
                    raise GuangYaNamingError("扫描项目数量超过安全上限")
                if child.is_dir:
                    if recursive:
                        queue.append(
                            (
                                child.file_id,
                                directory_path.rstrip("/") + "/" + child.name,
                            )
                        )
                    continue
                consider(child, directory_path)

    return _finalize_rename_plan(
        client,
        mode=safe_mode,
        recursive=recursive,
        targets=normalized_targets,
        limit=safe_limit,
        cache=cache,
        raw_entries=list(candidates.values()),
        scanned_items=scanned_items,
        scanned_dirs=scanned_dirs,
        no_change=no_change,
        transform={
            "find_text": find_text if safe_mode == "replace_text" else "",
            "replace_text": replace_text if safe_mode == "replace_text" else "",
        },
    )
