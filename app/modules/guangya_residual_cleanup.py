"""只读识别整理残留与真空目录；真实ID选择编译成cloud.change操作，不保存第二套计划。"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.clients.guangya import file_extension, GuangYaClient, GuangYaFile
from app.modules.guangya_rename import GuangYaNamingError
from app.modules.organize import DEFAULT_ORGANIZE_VIDEO_EXTS
from app.clients.guangya import file_snapshot
from app.modules.local_media_cleanup import SYSTEM_JUNK_NAMES

_MAX_SCANNED_ITEMS = 100_000
_MAX_SCANNED_DIRS = 5_000
_MAX_REVIEW_FILES = 8
_MAX_RESIDUAL_DIRS = 64
_MAX_RESIDUAL_BYTES = 128 * 1024 * 1024
_FORBIDDEN_NAME_RE = re.compile(r"[\\/:*?\"<>|\x00-\x1f]+")
_DOMAIN_RE = re.compile(
    r"(?i)(?:https?://)?(?:www\.)?"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:com|net|org|tv|cc|me|cn|xyz|site|club|info|top|vip|pro|io)"
)
_AD_TOKEN_RE = re.compile(
    r"(?i)(?:广告|推广|扫码|二维码|最新地址|更多资源|防失联|发布页|解压密码|"
    r"app下载|website|homepage|download|readme|url)"
)
_IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif", "bmp"}
_LINK_EXTS = {"url", "website", "html", "htm"}
_TEXT_EXTS = {"txt"}
_PROTECTED_MEDIA_IMAGE_STEMS = {
    "poster",
    "fanart",
    "cover",
    "folder",
    "thumb",
    "thumbnail",
    "backdrop",
    "banner",
    "landscape",
    "logo",
    "clearlogo",
    "disc",
    "discart",
}

class GuangYaCleanupError(GuangYaNamingError):
    """清理范围或候选事实无效，未提交任何云盘写入。"""


@dataclass
class _Node:
    item: GuangYaFile
    source_index: int
    source_name: str
    parent_path: str
    path: str
    children: list[str] = field(default_factory=list)
    files: list[GuangYaFile] = field(default_factory=list)
    protected: bool = False

@dataclass
class _TreeSummary:
    file_count: int = 0
    dir_count: int = 1
    total_size: int = 0
    has_video: bool = False
    has_unsafe: bool = False

    @property
    def qualifies(self) -> bool:
        return (
            self.file_count > 0
            and not self.has_video
            and not self.has_unsafe
            and self.file_count <= _MAX_REVIEW_FILES
            and self.dir_count <= _MAX_RESIDUAL_DIRS
            and self.total_size <= _MAX_RESIDUAL_BYTES
        )

def _safe_public_name(value: object, fallback: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = _FORBIDDEN_NAME_RE.sub(" ", text)
    text = " ".join(text.split()).strip(" .-_")
    return (text or fallback)[:120].rstrip(" .-_") or fallback

def _configured_video_exts() -> set[str]:
    from app import config

    raw = (config.get("GY_ORGANIZE_VIDEO_EXTS", "") or "")
    if not raw.strip():
        return set(DEFAULT_ORGANIZE_VIDEO_EXTS)
    values = {
        item.strip().lower().lstrip(".")
        for item in re.split(r"[,，\s]+", raw)
        if re.fullmatch(r"[A-Za-z0-9]{1,10}", item.strip().lstrip("."))
    }
    return values or set(DEFAULT_ORGANIZE_VIDEO_EXTS)

def _supports_guarded_empty_delete(client: Any) -> bool:
    explicit = getattr(client, "supports_guarded_empty_directory_delete", None)
    if explicit is None:
        explicit = getattr(client, "supports_atomic_empty_directory_delete", None)
    if explicit is not None:
        return bool(explicit)
    return callable(getattr(client, "delete_empty_directory", None))

def _is_cleanup_review_candidate(item: GuangYaFile) -> bool:
    """只判断文件是否可以交给 Agent 按文件名复核，不直接判定为垃圾。"""
    name = (item.name or "")
    lowered = name.casefold()
    if lowered in SYSTEM_JUNK_NAMES:
        return True
    ext = file_extension(item)
    stem = re.sub(r"[.。．][^.。．]+$", "", name).strip().casefold()
    if ext in _LINK_EXTS:
        return True
    if ext in _TEXT_EXTS:
        return bool(_DOMAIN_RE.search(name) or _AD_TOKEN_RE.search(name))
    if ext in _IMAGE_EXTS:
        normalized_stem = re.sub(r"[^a-z0-9]+", "", stem)
        if any(token in normalized_stem for token in _PROTECTED_MEDIA_IMAGE_STEMS):
            return False
        return True
    return False

def _signature(entries: list[dict[str, Any]]) -> str:
    return hashlib.sha256(
        json.dumps(
            sorted(
                entries,
                key=lambda row: (
                    row["parent_id"],
                    row["name"].casefold(),
                    row["file_id"],
                ),
            ),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

def _collect_tree_entries(
    node_id: str, nodes: dict[str, _Node]
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    stack = [node_id]
    while stack:
        current_id = stack.pop()
        node = nodes[current_id]
        entries.append(file_snapshot(node.item))
        entries.extend(file_snapshot(item) for item in node.files)
        stack.extend(reversed(node.children))
    return entries

def inspect_cleanup(
    client: GuangYaClient,
    *,
    sources: list[dict[str, str]],
    protected_ids: set[str] | None = None,
) -> dict[str, Any]:
    normalized_sources: list[dict[str, str]] = []
    for index, source in enumerate(sources, start=1):
        source_id = (source.get("id") or "").strip()
        if not source_id or source_id == "0":
            raise GuangYaCleanupError("整理来源 ID 无效")
        if all(item["id"] != source_id for item in normalized_sources):
            normalized_sources.append(
                {
                    "id": source_id,
                    "name": (source.get("name") or f"来源{index}"),
                }
            )
    sources = normalized_sources
    if not sources:
        raise GuangYaCleanupError("未配置光鸭整理来源")
    video_exts = _configured_video_exts()
    nodes: dict[str, _Node] = {}
    source_children: dict[int, list[str]] = {}
    scanned_items = 0
    scanned_dirs = 0
    protected_source_ids = {
        (source.get("id") or "").strip()
        for source in sources
        if (source.get("id") or "").strip()
    }
    protected_source_ids.update(protected_ids or ())
    protected_boundaries = 0

    for source_index, source in enumerate(sources, start=1):
        source_id = (source.get("id") or "").strip()
        source_name = (source.get("name") or f"来源{source_index}")
        queue = deque([(source_id, "/")])
        seen = {source_id}
        source_children[source_index] = []
        while queue:
            parent_id, parent_path = queue.popleft()
            items = client.list_dir(parent_id)
            for item in items:
                if item.file_id in seen or item.parent_id != parent_id:
                    raise GuangYaCleanupError("来源清单包含重复身份或父目录漂移，请重新读取")
                seen.add(item.file_id)
                scanned_items += 1
                if scanned_items > _MAX_SCANNED_ITEMS:
                    raise GuangYaCleanupError("整理来源项目数量超过安全上限")
                if not item.is_dir:
                    if parent_id in nodes:
                        nodes[parent_id].files.append(item)
                    continue
                if item.file_id in protected_source_ids:
                    protected_boundaries += 1
                    if parent_id in nodes:
                        nodes[parent_id].protected = True
                    continue
                scanned_dirs += 1
                if scanned_dirs > _MAX_SCANNED_DIRS:
                    raise GuangYaCleanupError("整理来源目录数量超过安全上限")
                path = parent_path.rstrip("/") + "/" + item.name
                node = _Node(item, source_index, source_name, parent_path, path)
                nodes[item.file_id] = node
                if parent_id in nodes:
                    nodes[parent_id].children.append(item.file_id)
                else:
                    source_children[source_index].append(item.file_id)
                queue.append((item.file_id, path))

    summaries: dict[str, _TreeSummary] = {}
    for node_id in reversed(list(nodes)):
        node = nodes[node_id]
        summary = _TreeSummary(has_unsafe=node.protected)
        for item in node.files:
            summary.file_count += 1
            summary.total_size += max(0, (item.size or 0))
            if file_extension(item) in video_exts:
                summary.has_video = True
            elif not _is_cleanup_review_candidate(item):
                summary.has_unsafe = True
        for child_id in node.children:
            child = summaries[child_id]
            summary.file_count += child.file_count
            summary.dir_count += child.dir_count
            summary.total_size += child.total_size
            summary.has_video = summary.has_video or child.has_video
            summary.has_unsafe = summary.has_unsafe or child.has_unsafe
        summaries[node_id] = summary

    residual_ids: list[str] = []
    empty_ids: list[str] = []
    preserved_dirs = protected_boundaries
    unsupported_empty_dirs = 0
    supports_empty_delete = _supports_guarded_empty_delete(client)

    def select(node_id: str, blocked: bool = False) -> None:
        nonlocal preserved_dirs, unsupported_empty_dirs
        node = nodes[node_id]
        summary = summaries[node_id]
        if not blocked and summary.qualifies:
            residual_ids.append(node_id)
            return
        if not node.files and not node.children and not node.protected:
            if not supports_empty_delete:
                unsupported_empty_dirs += 1
                preserved_dirs += 1
            elif (node.item.etag or "") or (node.item.updated_at or 0) > 0:
                empty_ids.append(node_id)
            else:
                preserved_dirs += 1
            return
        if summary.has_video or summary.has_unsafe or node.files:
            preserved_dirs += 1
        for child_id in node.children:
            select(child_id, blocked=False)

    for child_ids in source_children.values():
        for node_id in child_ids:
            select(node_id)

    candidates = []
    for node_id in residual_ids:
        node = nodes[node_id]
        tree = _collect_tree_entries(node_id, nodes)
        candidates.append({"file_id": node_id, "root": file_snapshot(node.item), "tree": tree,
                           "signature": _signature(tree), "source_id": sources[node.source_index - 1]["id"],
                           "file_count": summaries[node_id].file_count, "total_size": summaries[node_id].total_size,
                           "file_names": [entry["name"] for entry in tree if not entry["is_dir"]]})
    empties = [{"file_id": node_id, "root": file_snapshot(nodes[node_id].item), "depth": nodes[node_id].path.count("/")}
               for node_id in empty_ids]
    empties.sort(key=lambda row: (-row["depth"], row["root"]["name"].casefold()))
    return {"sources": sources, "candidates": candidates, "empty_directories": empties,
            "stats": {"source_count": len(sources), "scanned_items": scanned_items, "scanned_dirs": scanned_dirs,
                      "empty_dir_count": len(empties), "candidate_count": len(candidates),
                      "preserved_dir_count": preserved_dirs, "unsupported_empty_dir_count": unsupported_empty_dirs}}


def configured_cleanup_sources() -> list[dict[str, str]]:
    from app import config
    from app.modules.organize_sources import normalize_organize_sources
    from app.modules.offline import OfflineRules
    sources, error = normalize_organize_sources(config.get("GY_ORGANIZE_SOURCE_DIRS", ""))
    if error:
        raise GuangYaCleanupError(error)
    rules = OfflineRules.from_config()
    extras = [(rules.target_dir_id, rules.target_dir_name)]
    if rules.secondary_enabled:
        extras.append((rules.secondary_dir_id, rules.secondary_dir_name))
    for identifier, name in extras:
        if identifier and identifier != "0" and all(source["id"] != identifier for source in sources):
            sources.append({"id": identifier, "name": name or "光鸭离线目录"})
    return sources


def preview_cleanup(client: GuangYaClient, source_ids: list[str] | None = None, quarantine_ids: list[str] | None = None,
                    *, include_empty: bool = True, limit: int = 100, offset: int = 0) -> dict:
    """逐项决定改用真实ID；未选择的非空目录始终保留，无候选编号或私有计划。"""
    from app.modules.guangya_workspace import file_location
    for values in (source_ids, quarantine_ids):
        if values is not None and (not isinstance(values, list) or any(not isinstance(item, str) or not item or item == "0" for item in values)):
            raise GuangYaCleanupError("来源/隔离选择必须是真实非根ID数组")
    if type(include_empty) is not bool or type(limit) is not int or not 1 <= limit <= 500 or type(offset) is not int or offset < 0:
        raise GuangYaCleanupError("分页或include_empty参数无效")
    configured = configured_cleanup_sources()
    sources = configured if source_ids is None else [{"id": item, "name": file_location(client, item)["name"]} for item in dict.fromkeys(source_ids)]
    for source in sources:
        if not file_location(client, source["id"])["is_dir"]:
            raise GuangYaCleanupError("清理来源必须是目录")
    inspected = inspect_cleanup(client, sources=sources, protected_ids={source["id"] for source in configured})
    by_id = {item["file_id"]: item for item in inspected["candidates"]}
    selected = list(dict.fromkeys(quarantine_ids or ()))
    if any(identifier not in by_id for identifier in selected):
        raise GuangYaCleanupError("所选隔离ID已变化或不属于当前安全候选，请重新读取")
    operations = [{"op": "trash", "file_id": item["file_id"], "expected": item["root"], "require_empty": True}
                  for item in inspected["empty_directories"]] if include_empty else []
    if selected:
        names = ["MediaFlux隔离", "整理残留", datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]]
        path, current_id, pending = "/", "0", False
        for name in names:
            found = [] if pending else [item for item in client.list_dir(current_id) if item.name == name]
            if len(found) > 1 or (found and not found[0].is_dir):
                raise GuangYaCleanupError("隔离目录路径存在歧义或被文件占用")
            if found:
                current_id = found[0].file_id
            else:
                operations.append({"op": "create_directory", "parent_path": path, "name": name})
                pending = True
            path = path.rstrip("/") + "/" + name
        for index, identifier in enumerate(selected, 1):
            candidate = by_id[identifier]
            name = f"{index:03d}-" + _safe_public_name(candidate["root"]["name"], "残留目录")
            operations.append({"op": "create_directory", "parent_path": path, "name": name})
            operations.append({"op": "move", "file_id": identifier, "target_path": path + "/" + name,
                               "expected": candidate["root"], "expected_tree": candidate["tree"]})
    total = len(inspected["candidates"])
    page = inspected["candidates"][offset:offset + limit]
    return {"status": "ready" if operations else "selection_required" if total else "no_changes", "sources": sources,
            "candidates": page, "total": total, "returned": len(page), "offset": offset, "limit": limit,
            "has_more": offset + len(page) < total, "next_offset": offset + len(page) if offset + len(page) < total else None,
            "empty_directories": inspected["empty_directories"], "selected_candidates": [by_id[item] for item in selected],
            "stats": {**inspected["stats"], "selected_count": len(selected), "kept_count": total - len(selected)},
            "operations": operations, "cloud_write": False}
