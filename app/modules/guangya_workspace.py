"""光鸭真实身份读取、完整分页与路径边界；不保存目录观察或会话状态。"""
from __future__ import annotations
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import asdict
from typing import Any
from app.clients.guangya import file_extension, GuangYaClient, GuangYaFile
from app.modules.organize import DEFAULT_ORGANIZE_METADATA_EXTS, DEFAULT_ORGANIZE_VIDEO_EXTS
from app.modules.organize_postprocess import SUBTITLE_EXTS

_MAX_DEPTH = 64
_IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif", "bmp", "avif"}


class GuangYaWorkspaceError(RuntimeError):
    """完整目录读取或路径核对失败。"""


class GuangYaWorkspaceStale(GuangYaWorkspaceError):
    """真实对象身份或目录边界已变化。"""


def file_location(client: GuangYaClient, file_id: str, *, check_cancelled: Callable[[], None] = lambda: None) -> dict:
    """按真实ID核验父链，不创建观察引用或会话游标。"""
    if not isinstance(file_id, str) or not file_id.strip() or file_id == "0":
        raise GuangYaWorkspaceError("必须指定非账号根的真实文件或目录ID")
    check_cancelled()
    item = client.file_info(file_id)
    if item is None or item.file_id != file_id:
        raise GuangYaWorkspaceStale("文件或目录不存在，不能按名称替代")
    located = locate_file_in_roots(client, {"file_id": file_id, "is_dir": item.is_dir},
        [{"file_id": "0", "path": "/", "is_dir": True}], cache={file_id: item}, check_cancelled=check_cancelled)
    if located is None:
        raise GuangYaWorkspaceStale("无法完整核对对象父目录，请重新读取")
    return {**asdict(item), "parent_path": located["parent_path"],
            "path": str(located["parent_path"]).rstrip("/") + "/" + item.name}


def list_files(client: GuangYaClient, parent_id: str = "0", *, recursive: bool = False, query: str = "",
               kinds: list[str] | None = None, limit: int | None = None, offset: int = 0,
               max_items: int = 100_000, max_depth: int = 64, max_dirs: int = 5_000,
               should_stop: Callable[[], bool] | None = None) -> dict:
    """完整读取所选范围后过滤/分页；深度或数量预算不足时抛错，绝不伪装全量。"""
    if not isinstance(parent_id, str) or not parent_id or type(recursive) is not bool or not isinstance(query, str):
        raise GuangYaWorkspaceError("目录ID、recursive或query参数无效")
    if (type(offset) is not int or offset < 0 or (limit is not None and (type(limit) is not int or not 1 <= limit <= 10_000))
            or type(max_items) is not int or not 1 <= max_items <= 100_000
            or type(max_depth) is not int or not 0 <= max_depth <= 64
            or type(max_dirs) is not int or not 1 <= max_dirs <= 5_000):
        raise GuangYaWorkspaceError("分页或扫描预算无效")
    if kinds is not None and (not isinstance(kinds, list) or any(kind not in {"directory", "video", "subtitle", "image", "metadata", "other"} for kind in kinds)):
        raise GuangYaWorkspaceError("kinds包含未知媒体类型")
    def check_cancelled():
        if should_stop and should_stop():
            raise InterruptedError("云盘目录读取已停止，未返回完整清单")

    check_cancelled()
    root: dict[str, Any] = {"file_id": "0", "is_dir": True, "path": "/"}
    if parent_id.startswith(("/", "\\")):
        path = _normalize_path(parent_id, allow_root=True)
        if path == "/":
            parent_id = "0"
        else:
            item, parent_path = _resolve_path(client, path, should_stop=should_stop, check_cancelled=check_cancelled)
            parent_id = item.file_id
            root = {**asdict(item), "parent_path": parent_path, "path": parent_path.rstrip("/") + "/" + item.name}
    elif parent_id != "0":
        root = file_location(client, parent_id, check_cancelled=check_cancelled)
    if not root["is_dir"]:
        raise GuangYaWorkspaceError("list只能读取目录；单个文件使用cloud.info")
    queue = deque([(parent_id, root["path"], "", 0)])
    seen, rows, directories = {parent_id}, [], 0
    while queue:
        check_cancelled()
        identifier, path, relative, depth = queue.popleft()
        directories += 1
        for item in client.iter_dir(identifier, should_stop=should_stop):
            check_cancelled()
            if item.file_id in seen or item.parent_id != identifier:
                raise GuangYaWorkspaceStale("目录清单包含重复身份或父目录漂移，请重新读取")
            seen.add(item.file_id)
            if len(seen) - 1 > max_items:
                raise GuangYaWorkspaceError("项目数量超过完整读取预算，请缩小范围，未返回截断快照")
            child_relative = relative + "/" + item.name if relative else item.name
            row = {**asdict(item), "path": path.rstrip("/") + "/" + item.name,
                   "relative_path": child_relative, "parent_path": path, "media_kind": _media_kind(item)}
            rows.append(row)
            if recursive and item.is_dir:
                if depth >= max_depth:
                    raise GuangYaWorkspaceError("递归深度超过完整读取预算，请缩小范围")
                if directories + len(queue) >= max_dirs:
                    raise GuangYaWorkspaceError(
                        f"范围超过 {max_dirs} 个目录的读取预算，未返回不完整清单；"
                        "请用目录ID或绝对路径缩小parent_id范围。query只过滤结果，不减少目录扫描。"
                    )
                queue.append((item.file_id, row["path"], child_relative, depth + 1))
        check_cancelled()  # iter_dir取消时可提前结束，不能把半份目录当成完整快照。
    matches = [row for row in rows if (not query or query.casefold() in row["name"].casefold())
               and (not kinds or row["media_kind"] in kinds)]
    page = matches[offset:offset + limit] if limit is not None else matches[offset:]
    more = offset + len(page) < len(matches)
    return {"parent_id": parent_id, "root": root, "recursive": recursive, "items": page, "total": len(matches),
            "returned": len(page), "offset": offset, "limit": limit, "has_more": more,
            "next_offset": offset + len(page) if more else None, "scan_complete": True, "scanned_items": len(rows),
            "scanned_dirs": directories}


def _normalize_path(value: object, *, allow_root: bool = False) -> str:
    path = str(value or "").strip().replace("\\", "/")
    if not path.startswith("/") or len(path) > 2048:
        raise GuangYaWorkspaceError("光鸭目录路径必须是绝对路径")
    parts = [part for part in path.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise GuangYaWorkspaceError("光鸭目录路径不能包含相对组件")
    if not parts:
        if allow_root:
            return "/"
        raise GuangYaWorkspaceError("该操作不能以光鸭根目录为对象")
    return "/" + "/".join(parts)


def _resolve_path(client: GuangYaClient, path: str, *, should_stop: Callable[[], bool] | None = None,
                  check_cancelled: Callable[[], None] = lambda: None) -> tuple[GuangYaFile, str]:
    components = _normalize_path(path).strip("/").split("/")
    current: GuangYaFile | None = None
    parent_id = "0"
    parent_path = "/"
    for index, component in enumerate(components):
        check_cancelled()
        items = list(client.iter_dir(parent_id, should_stop=should_stop))
        check_cancelled()
        exact = [item for item in items if item.name == component]
        matches = exact or [
            item for item in items if item.name.casefold() == component.casefold()
        ]
        if len(matches) != 1:
            raise GuangYaWorkspaceError(
                f"路径不存在或名称不唯一：/{'/'.join(components[: index + 1])}"
            )
        if current is not None:
            parent_path = parent_path.rstrip("/") + "/" + current.name
        current = matches[0]
        if index < len(components) - 1 and not current.is_dir:
            raise GuangYaWorkspaceError("光鸭路径的中间组件不是目录")
        parent_id = current.file_id
    assert current is not None
    return current, parent_path


def resolve_workspace_path(client: GuangYaClient, path: object) -> GuangYaFile:
    """按精确绝对路径核对唯一对象；公开事实仍使用真实ID。"""
    target, _parent_path = _resolve_path(client, _normalize_path(path))
    return target


def _media_kind(item: GuangYaFile) -> str:
    if item.is_dir:
        return "directory"
    ext = file_extension(item)
    if ext in DEFAULT_ORGANIZE_VIDEO_EXTS:
        return "video"
    if ext in SUBTITLE_EXTS:
        return "subtitle"
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in DEFAULT_ORGANIZE_METADATA_EXTS:
        return "metadata"
    return "other"


def _snapshot(
    item: GuangYaFile, *, parent_path: str, relative_parent: str
) -> dict[str, Any]:
    return {
        "file_id": item.file_id,
        "parent_id": item.parent_id,
        "name": item.name,
        "is_dir": item.is_dir,
        "size": max(0, (item.size or 0)),
        "etag": (item.etag or ""),
        "updated_at": max(0, (item.updated_at or 0)),
        "extension": file_extension(item),
        "media_kind": _media_kind(item),
        "parent_path": parent_path,
        "relative_parent": relative_parent,
    }


def locate_file_in_roots(client: GuangYaClient, expected: dict[str, Any], roots: Sequence[dict[str, Any]],
                           *, cache: dict[str, GuangYaFile | None], check_cancelled: Callable[[], None]) -> dict[str, Any] | None:
    """只追踪原选中 ID，以有界父链证明仍在原根；不搜索其它对象或全盘。"""
    def read(file_id: str) -> GuangYaFile | None:
        check_cancelled()
        if file_id not in cache:
            cache[file_id] = client.file_info(file_id)
        check_cancelled()
        return cache[file_id]

    file_id = str(expected.get("file_id") or "")
    current = read(file_id)
    if current is None or current.file_id != file_id or current.is_dir != bool(expected.get("is_dir")):
        return None
    by_id = {str(root["file_id"]): root for root in roots if root.get("is_dir") is True}
    parent_id = (current.parent_id or "0")
    names = [current.name]
    visited = {file_id}
    for _ in range(_MAX_DEPTH + 1):
        check_cancelled()
        if parent_id in by_id:
            root = by_id[parent_id]
            path = str(root["path"]).rstrip("/") + "/" + "/".join(reversed(names))
            path = _normalize_path(path)
            entry = _snapshot(current, parent_path=path.rsplit("/", 1)[0] or "/",
                              relative_parent=" › ".join(reversed(names[1:])) or "当前目录")
            return entry
        if parent_id == "0" or parent_id in visited:
            return None
        visited.add(parent_id)
        parent = read(parent_id)
        if parent is None or parent.file_id != parent_id or not parent.is_dir:
            return None
        names.append(parent.name)
        parent_id = (parent.parent_id or "0")
    return None
