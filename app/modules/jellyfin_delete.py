"""Jellyfin 主动删除：索引确定身份，统一 Writer 内校验并移入回收站。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from app import config
from app.clients.guangya import GuangYaClient, close_guangya_client
from app.logger import get_logger
from app.modules.directory_scrape_errors import (
    DirectoryScrapeConflictError,
    DirectoryScrapePublicError,
    DirectoryScrapeRequestError,
    public_error_message,
)
from app.modules.download_staging_cleanup import assert_guangya_download_delete_allowed
from app.modules.media_server_path_mapping import (
    parse_media_server_path_mappings,
    reverse_media_server_path_mapping,
)
from app.modules.organize import Organizer, OrganizeRules
from app.modules.organize_delete_audit import (
    DeleteCandidate,
    execute_recycle_bin_delete,
    record_blocked_delete,
)
from app.modules.organize_rules import DEFAULT_ORGANIZE_VIDEO_EXTS
from app.modules.organize_tasks import _protected_organize_root_ids
from app.modules.strm import (
    STRM_OPERATION_LOCK,
    STRM_SUBDIR,
    _fingerprint_matches,
    _strm_target,
    configured_strm_source_plans,
    safe_path_component,
)
from app.repositories.strm import list_strm_file_owners, list_strm_path_owners, retire_deleted_strm_files

logger = get_logger(__name__)
MAX_DELETE_PATHS = 1000
DELETE_SCOPE_KINDS = ("movie", "episode", "season", "series", "folder")


@dataclass(frozen=True)
class DeleteTarget:
    path: str
    source_id: str
    file_id: str
    name: str
    etag: str
    size: int
    fingerprint: str
    scope_kind: str = "file"
    scope_root: str = ""


def _indexed_target(path: str, row) -> DeleteTarget:
    return DeleteTarget(
        path, str(row["source"]).removeprefix("guangya:"), str(row["file_id"]),
        str(row["filename"]), str(row["etag"]), int(row["size"]), str(row["content_fingerprint"]),
    )


def _scopes(payload: dict, mappings, local_paths: dict[str, str]) -> dict[str, tuple[str, str]]:
    """目录权限只能来自显式媒体范围；旧文件请求不隐式获得目录权限。"""
    if "scopes" not in payload:
        return {path: ("file", "") for path in local_paths}
    scopes = payload["scopes"]
    if not isinstance(scopes, list) or not 1 <= len(scopes) <= MAX_DELETE_PATHS:
        raise DirectoryScrapeRequestError("媒体删除范围无效")
    result = {}
    for scope in scopes:
        if not isinstance(scope, dict) or scope.get("kind") not in DELETE_SCOPE_KINDS:
            raise DirectoryScrapeRequestError("请选择电影、单集、整季、整剧或明确文件夹删除范围")
        kind, raw_root, members = scope["kind"], scope.get("root"), scope.get("paths")
        if raw_root is not None and (not isinstance(raw_root, str) or not raw_root.strip()):
            raise DirectoryScrapeRequestError("作品目录必须是有效路径或 null")
        if not isinstance(members, list) or not members or len(members) > MAX_DELETE_PATHS:
            raise DirectoryScrapeRequestError("删除范围缺少 STRM 文件")
        if (kind == "episode" and raw_root) or (kind in {"season", "series", "folder"} and not raw_root):
            raise DirectoryScrapeRequestError("单集不能授权目录，整季/整剧/文件夹必须指定作品目录")
        root = ""
        try:
            if raw_root:
                root, mapping = reverse_media_server_path_mapping(raw_root, mappings)
                directory = Path(root)
                if directory.resolve() != directory or root == mapping.local_prefix:
                    raise ValueError("不能删除映射根目录或符号链接目录")
            for member in members:
                if not isinstance(member, str):
                    raise DirectoryScrapeRequestError("范围内 STRM 路径无效")
                path = reverse_media_server_path_mapping(member, mappings)[0]
                if path not in local_paths or path in result:
                    raise ValueError("删除范围与选中的 STRM 不一致或重复")
                if root and (not Path(path).is_relative_to(root) or Path(path) == Path(root)):
                    raise ValueError("STRM 不在已授权的作品目录内")
                if kind == "movie" and root and Path(path).parent != Path(root):
                    raise ValueError("电影目录必须是视频的直接父目录")
                result[path] = (kind, root)
        except ValueError as exc:
            raise DirectoryScrapeRequestError(str(exc)) from exc
    if set(result) != set(local_paths):
        raise DirectoryScrapeRequestError("每个 STRM 必须属于明确的删除范围")
    return result


def prepare_delete(payload: dict) -> tuple[DeleteTarget, ...]:
    """核对 STRM 身份与显式媒体范围；目录不能替代 STRM 证据或提交裸云盘 ID。"""
    paths = payload.get("paths")
    if not isinstance(paths, list) or not 1 <= len(paths) <= MAX_DELETE_PATHS:
        raise DirectoryScrapeRequestError(f"请选择 1–{MAX_DELETE_PATHS} 个 STRM 文件")
    if any(not isinstance(path, str) or not path.strip() or len(path) > 4096 for path in paths):
        raise DirectoryScrapeRequestError("STRM 路径无效")
    fingerprints = payload.get("fingerprints")
    if not isinstance(fingerprints, dict) or any(
        not isinstance(fingerprints.get(path), str) or len(fingerprints[path]) != 71
        for path in paths
    ):
        raise DirectoryScrapeRequestError("请提供 Jellyfin 实际 STRM 文件的 SHA256 指纹")
    mode = payload.get("mapping_mode", "mf")
    if mode == "mf":
        raw_mappings = config.get("JELLYFIN_PATH_MAPPINGS", "")
    elif mode == "manual" and isinstance(payload.get("mappings"), list):
        raw_mappings = json.dumps(payload["mappings"], ensure_ascii=False)
    else:
        raise DirectoryScrapeRequestError("请选择 MF 映射或手动映射")
    try:
        mappings = parse_media_server_path_mappings(raw_mappings)
        local_paths = {}
        for path in paths:
            local_path = reverse_media_server_path_mapping(path, mappings)[0]
            fingerprint = fingerprints[path].lower()
            if local_path in local_paths and local_paths[local_path] != fingerprint:
                raise DirectoryScrapeRequestError("同一 MF 路径对应的 STRM 指纹不一致")
            local_paths[local_path] = fingerprint
    except ValueError as exc:
        raise DirectoryScrapeRequestError(str(exc)) from exc

    root_text = (config.get("STRM_ROOT", "") or "").strip()
    if not root_text:
        raise DirectoryScrapeConflictError("尚未配置 STRM 输出目录")
    root = (Path(root_text).expanduser() / STRM_SUBDIR).resolve()
    sources, source_error = configured_strm_source_plans()
    if source_error:
        raise DirectoryScrapeConflictError("STRM 来源配置无效，请先检查 MF 配置")
    source_ids = {source["id"] for source in sources}
    scopes = _scopes(payload, mappings, local_paths)
    targets = []
    for text in sorted(local_paths):
        path = Path(text)
        if not path.is_absolute() or path.suffix.lower() != ".strm":
            raise DirectoryScrapeRequestError("只能删除已纳管的 STRM 文件，不能提交目录")
        # 不沿符号链接删除目标身份；MF/Jellyfin 必须明确指向同一纳管文件。
        if path.resolve() != path or not path.is_relative_to(root):
            raise DirectoryScrapeConflictError("STRM 路径越界或经过符号链接")
        rows = list_strm_path_owners(str(path))
        if len(rows) != 1:
            raise DirectoryScrapeConflictError("STRM 索引缺失或存在多个所有者，请先同步核对")
        row = dict(rows[0])
        source = str(row["source"])
        source_id = source.removeprefix("guangya:")
        if not source.startswith("guangya:") or source_id not in source_ids:
            raise DirectoryScrapeConflictError("文件不属于当前配置的光鸭 STRM 来源")
        if not row["file_id"] or str(row["file_id"]) == "0" or not row["etag"]:
            raise DirectoryScrapeConflictError("STRM 索引缺少云端文件身份，请先同步")
        if str(row["content_fingerprint"]) != local_paths[text]:
            raise DirectoryScrapeConflictError("Jellyfin 与 MF 的 STRM 内容不一致，请检查路径映射")
        if not _fingerprint_matches(path, row["content_fingerprint"]):
            raise DirectoryScrapeConflictError("STRM 内容已变化或文件不存在，请先同步核对")
        kind, scope_root = scopes[str(path)]
        targets.append(replace(_indexed_target(str(path), row), scope_kind=kind, scope_root=scope_root))
    for file_id in {target.file_id for target in targets}:
        if any(str(row["strm_path"]) not in local_paths for row in list_strm_file_owners(file_id)):
            raise DirectoryScrapeConflictError("云端文件还被其他 STRM 引用，不能单独联动删除")
    return tuple(targets)


def delete_key(targets: tuple[DeleteTarget, ...]) -> str:
    identities = [(item.path, item.file_id, item.etag, item.fingerprint, item.scope_kind, item.scope_root)
                  for item in targets]
    return "jellyfin-delete:" + hashlib.sha256(json.dumps(identities).encode()).hexdigest()


def _snapshot(item) -> tuple:
    return (item.file_id, item.name, item.parent_id, item.is_dir) + (
        () if item.is_dir else (item.size, item.etag)
    )


def _verify_cloud_file(client, target: DeleteTarget, directories: dict, parents: dict, frozen_dirs=None):
    item = client.file_info(target.file_id)
    if item is None or item.is_dir or str(item.file_id) != target.file_id:
        raise DirectoryScrapeConflictError("云端文件已不存在或类型变化；不会重试回收站删除")
    if (item.name, str(item.etag), int(item.size)) != (target.name, target.etag, target.size):
        raise DirectoryScrapeConflictError("云端文件与 STRM 索引不一致，请先同步核对")
    current = item
    ancestors = {"0"}
    visited = {target.file_id}
    while True:
        if frozen_dirs and current.file_id in frozen_dirs and _snapshot(current) != _snapshot(frozen_dirs[current.file_id]):
            raise DirectoryScrapeConflictError("作品目录归属或名称已变化，已停止删除")
        parent_id = str(current.parent_id)
        if not parent_id or parent_id in visited or len(visited) > 256:
            raise DirectoryScrapeConflictError("无法确认云端文件的正常目录归属")
        # file_info 可能仍能读到回收站对象；必须在正常祖先链的目录列表中确认。
        if parent_id not in directories:
            directories[parent_id] = {str(child.file_id): child for child in client.list_dir(parent_id)}
        listed = directories[parent_id].get(str(current.file_id))
        if listed is None or _snapshot(listed) != _snapshot(current):
            raise DirectoryScrapeConflictError("云端文件或目录已变化，或已在回收站中；已取消删除")
        ancestors.add(parent_id)
        if parent_id == "0":
            break
        visited.add(parent_id)
        if parent_id not in parents:
            parents[parent_id] = client.file_info(parent_id)
        current = parents[parent_id]
        if current is None or not current.is_dir or str(current.file_id) != parent_id:
            raise DirectoryScrapeConflictError("无法确认云端文件的父目录")
    if target.source_id not in ancestors:
        raise DirectoryScrapeConflictError("云端文件已离开已配置 STRM 来源，已取消删除")
    candidate = DeleteCandidate(item.file_id, item.name, item.parent_id, item.size, item.etag)
    try:
        assert_guangya_download_delete_allowed(client, item)
    except DirectoryScrapeConflictError as exc:
        record_blocked_delete(trigger="jellyfin_delete", reason=public_error_message(exc), candidate=candidate)
        raise
    return candidate


def _video_extensions() -> set[str]:
    extensions = set(DEFAULT_ORGANIZE_VIDEO_EXTS) | {"strm", "iso"}
    extensions.update(Organizer._parse_exts((config.get("GY_ORGANIZE_VIDEO_EXTS", "") or ""), set()))
    return {"." + ext.lstrip(".") for ext in extensions}


def _companions(entries, selected: set[str], video_exts: set[str]):
    """最长同名视频归属，避免 E01 吞掉 E010/其他版本及剧级共享素材。"""
    owners_by_stem = {}
    for item in entries:
        if not item.is_dir and (Path(item.name).suffix.lower() in video_exts or item.file_id in selected):
            owners_by_stem.setdefault(Path(item.name).stem.casefold(), set()).add(item.file_id)
    for item in entries:
        extension = Path(item.name).suffix.lower()
        if item.is_dir or extension in video_exts or item.file_id in selected:
            continue
        name = item.name.casefold()
        separators = {".", "-"} if extension in {".jpg", ".jpeg", ".png", ".webp"} else {"."}
        for index in range(len(name) - 1, -1, -1):
            if name[index] in separators and name[:index] in owners_by_stem:
                if owners_by_stem[name[:index]] <= selected:
                    yield item
                break


def _protected_directories():
    protected, error = _protected_organize_root_ids(OrganizeRules.from_config())
    sources, source_error = configured_strm_source_plans()
    if error or source_error:
        raise DirectoryScrapeConflictError("来源保护配置无法核对，已取消删除")
    protected.update(source["id"] for source in sources)
    return protected, sources


def _cloud_plan(client, expected, candidates, listings, parents):
    """冻结明确范围内的文件清单；目录只在文件回收完成且真实为空时收尾。"""
    if all(target.scope_kind == "file" for target in expected):
        return {target.file_id: target for target in expected}, {}, set(), set(), {}, {}
    protected, sources = _protected_directories()
    source_plans = {str(source["id"]): source for source in sources}
    strm_root = config.get("STRM_ROOT", "")
    files = {target.file_id: target for target in expected}
    selected_media = frozenset(files)
    video_exts = _video_extensions()
    directories = {}
    full_roots = set()
    inventories, file_guards = {}, {}
    groups = {}
    for target in expected:
        if target.scope_kind != "file":
            key = (target.scope_kind, target.scope_root or candidates[target.file_id].parent_id)
            groups.setdefault(key, []).append(target)

    def add_file(item, source_id):
        if item.file_id not in files and len(files) >= 10000:
            raise DirectoryScrapeConflictError("删除范围文件过多，请分批操作")
        if not item.etag:
            raise DirectoryScrapeConflictError("伴随文件缺少云端身份，已取消删除")
        files.setdefault(item.file_id, DeleteTarget("", source_id, item.file_id, item.name, item.etag, item.size, ""))

    def freeze(directory_id, entries):
        snapshot = {item.file_id: _snapshot(item) for item in entries}
        if directory_id in inventories and inventories[directory_id] != snapshot:
            raise DirectoryScrapeConflictError("规划期间目录成员发生变化，已取消删除")
        inventories[directory_id] = snapshot

    entries_seen = 0
    for (kind, local_root), targets in groups.items():
        selected = {target.file_id for target in targets}
        root_ids = set()
        for target in targets:
            directory_id = candidates[target.file_id].parent_id
            if target.scope_root:
                # 目录权限必须绑定到实际云端层级，不能把旧索引路径或折叠路径
                # 的字面层数当作另一个云端目录的删除授权。
                cloud_parts, current = [], directory_id
                while current != target.source_id:
                    if current not in parents or len(cloud_parts) >= 64:
                        raise DirectoryScrapeConflictError("无法核对作品目录与 STRM 来源的层级")
                    cloud_parts.append(safe_path_component(parents[current].name))
                    current = parents[current].parent_id
                prefix = source_plans[target.source_id]["rel_prefix"]
                rel_parts = ([safe_path_component(prefix)] if prefix else []) + list(reversed(cloud_parts))
                item = listings[candidates[target.file_id].parent_id][target.file_id]
                if _strm_target(item, "/".join(rel_parts), strm_root) != Path(target.path):
                    raise DirectoryScrapeConflictError("作品目录与当前云端路径不一致，请先同步核对")
                steps = len(Path(target.path).relative_to(local_root).parts) - 1
                if kind in {"season", "series", "folder"} and len(Path(target.path).relative_to(Path(strm_root) / STRM_SUBDIR).parts) != len(rel_parts) + 1:
                    raise DirectoryScrapeConflictError("STRM 路径已折叠，不能据此扩大整季/整剧/文件夹删除范围")
                for _ in range(steps):
                    if directory_id in protected or directory_id == target.source_id:
                        raise DirectoryScrapeConflictError("不能扩大删除到来源根或归档根目录")
                    directory_id = parents[directory_id].parent_id
            root_ids.add(directory_id)
        if len(root_ids) != 1 or len({target.source_id for target in targets}) != 1:
            raise DirectoryScrapeConflictError("同一媒体范围不能跨云端目录或 STRM 来源")
        root_id = root_ids.pop()
        source_id = targets[0].source_id
        if targets[0].scope_root:
            if root_id in protected:
                raise DirectoryScrapeConflictError("不能删除来源根或归档根目录")
            tree_files, tree_dirs, tree_listings, stack = [], {}, {}, [(root_id, 0)]
            while stack:
                directory_id, depth = stack.pop()
                if directory_id in tree_dirs or depth > 64 or directory_id in protected:
                    raise DirectoryScrapeConflictError("作品目录包含受保护来源或目录结构异常")
                info = client.file_info(directory_id)
                if info is None or not info.is_dir:
                    raise DirectoryScrapeConflictError("作品目录已变化，已取消删除")
                assert_guangya_download_delete_allowed(client, info)
                tree_dirs[directory_id] = replace(info)
                children = client.list_dir(directory_id)
                tree_listings[directory_id] = children
                entries_seen += len(children)
                if entries_seen > 10000:
                    raise DirectoryScrapeConflictError("作品目录过大，请在 MF 中核对后分批操作")
                for item in children:
                    if str(item.parent_id) != str(directory_id):
                        raise DirectoryScrapeConflictError("作品目录成员归属不一致")
                    if item.is_dir:
                        stack.append((item.file_id, depth + 1))
                    else:
                        tree_files.append(item)
            # 同批其他 scope 已明确选中的媒体也有删除授权；例如父目录电影
            # 与其子目录电影一起选择，不能误当未选邻居而留下共享素材。
            selected.update(item.file_id for item in tree_files if item.file_id in selected_media)
            unselected_media = [item for item in tree_files if Path(item.name).suffix.lower() in video_exts and item.file_id not in selected]
            if not selected <= {item.file_id for item in tree_files}:
                raise DirectoryScrapeConflictError("选中媒体已不在作品目录内")
            if not unselected_media:
                owned = set(selected)
                for directory_id, children in tree_listings.items():
                    freeze(directory_id, children)
                    owned.update(item.file_id for item in _companions(children, selected, video_exts))
                for item in tree_files:
                    add_file(item, source_id)
                    # 同名伴随/媒体只依赖自身目录；共享海报等依赖整个所选作品。
                    file_guards.setdefault(item.file_id, set()).update(
                        {item.parent_id} if item.file_id in owned else tree_dirs.keys()
                    )
                directories.update(tree_dirs)
                full_roots.add(root_id)
                continue
            if kind != "movie":
                raise DirectoryScrapeConflictError("整季/整剧目录还有未选中或未同步的视频，已取消整批删除")
            # 混放电影只允许自身同名伴随文件，不能删除整目录或共享图片。
        for parent_id in {candidates[target.file_id].parent_id for target in targets}:
            entries = client.list_dir(parent_id)
            freeze(parent_id, entries)
            for item in _companions(entries, selected, video_exts):
                add_file(item, source_id)
                file_guards.setdefault(item.file_id, set()).add(parent_id)
            for target in targets:
                if candidates[target.file_id].parent_id == parent_id:
                    file_guards.setdefault(target.file_id, set()).add(parent_id)
            if parent_id not in protected:
                directories[parent_id] = replace(parents[parent_id])
    return files, directories, full_roots, protected, inventories, file_guards


def _check_inventory(client, inventories, directory_ids, removed: set[str], fresh_listings=None):
    """禁止用旧伴随归属处理新目录成员；复用本次写前读取，不重复扫描来源根。"""
    observed = fresh_listings if fresh_listings is not None else {}
    for directory_id in directory_ids:
        if directory_id not in observed:
            observed[directory_id] = {item.file_id: item for item in client.list_dir(directory_id)}
        actual = {file_id: _snapshot(item) for file_id, item in observed[directory_id].items()}
        expected = {file_id: snapshot for file_id, snapshot in inventories[directory_id].items()
                    if file_id not in removed}
        if actual != expected:
            raise DirectoryScrapeConflictError("目录成员或文件归属已变化，已停止删除并保留未处理文件")


def execute_delete(payload: dict, expected: tuple[DeleteTarget, ...]) -> dict:
    """统一 Writer 内先核验整批范围，再回收文件/伴随文件，最后只回收空目录。"""
    if prepare_delete(payload) != expected:
        raise DirectoryScrapeConflictError("映射或 STRM 索引已变化，请重新发起删除")
    client = GuangYaClient()
    completed = []
    retired_targets = []
    write_attempted = False
    error = ""
    stats: dict[str, Any] = {}
    try:
        if not client.logged_in:
            raise DirectoryScrapeConflictError("光鸭未登录，请先在 MF 登录")
        listings, parents = {}, {}
        candidates = {target.file_id: _verify_cloud_file(client, target, listings, parents) for target in expected}
        files, directories, full_roots, protected, inventories, file_guards = _cloud_plan(
            client, expected, candidates, listings, parents,
        )
        selected_paths = {target.path for target in expected}
        if prepare_delete(payload) != expected:
            raise DirectoryScrapeConflictError("删除期间映射或 STRM 内容发生变化，已停止后续删除")
        # 范围规划会读取目录：完成规划后重新复核整批，而不是先删第一项、
        # 到后续项才发现其早已变化。无范围扩展的旧文件请求已有同等预检。
        fresh_listings, fresh_parents = {}, {}
        for file_id, target in files.items():
            if inventories or file_id not in candidates:
                current = _verify_cloud_file(client, target, fresh_listings, fresh_parents, directories)
                if file_id in candidates and current != candidates[file_id]:
                    raise DirectoryScrapeConflictError("规划期间云端媒体已变化，已取消整批删除")
                candidates[file_id] = current
            if any(str(row["strm_path"]) not in selected_paths for row in list_strm_file_owners(file_id)):
                raise DirectoryScrapeConflictError("文件还被其他 STRM 引用，已取消整批删除")
        _check_inventory(client, inventories, inventories, set())
        if inventories and prepare_delete(payload) != expected:
            raise DirectoryScrapeConflictError("写入前 STRM 或媒体范围已变化，已取消整批删除")
        references = {}
        removed = set()
        for target in expected:
            references.setdefault(target.file_id, []).append(target)
        for file_id, target in files.items():
            if any(str(row["strm_path"]) not in selected_paths for row in list_strm_file_owners(file_id)):
                raise DirectoryScrapeConflictError("云端文件出现新的 STRM 引用，已停止后续删除")
            for original in references.get(file_id, []):
                rows = list_strm_path_owners(original.path)
                if (Path(original.path).resolve() != Path(original.path) or len(rows) != 1
                    or _indexed_target(original.path, rows[0]) != replace(original, scope_kind="file", scope_root="")
                    or not _fingerprint_matches(Path(original.path), original.fingerprint)):
                    raise DirectoryScrapeConflictError("执行前 STRM 身份发生变化，已停止后续删除")
            fresh_listings = {}
            candidate = _verify_cloud_file(client, target, fresh_listings, {}, directories)
            if candidate != candidates[file_id]:
                raise DirectoryScrapeConflictError("云端对象在执行前发生变化，已停止后续删除")
            _check_inventory(client, inventories, file_guards.get(file_id, ()), removed, fresh_listings)
            write_attempted = True
            audit = execute_recycle_bin_delete(
                client, trigger="jellyfin_delete", reason="Jellyfin 用户主动确认删除媒体及其所属伴随文件",
                candidate=candidate, safe_failure_message="云端删除结果未能确认，请在 MF 核对删除审计，不要盲目重试",
            )
            completed.append({"file_id": file_id, "audit_id": audit["audit_id"]})
            retired_targets.extend(asdict(item) for item in references.get(file_id, [target]))
            removed.add(file_id)
        # 不调用递归目录删除。云端新增内容会让安全空目录删除拒绝，绝不会被顺带删除。
        organizer = Organizer(client=client, recognition_enabled=False)
        def depth(directory_id):
            count, current = 0, directory_id
            while current in directories:
                count += 1
                current = directories[current].parent_id
            return count
        for directory_id in sorted(directories, key=depth, reverse=True):
            info = client.file_info(directory_id)
            if info is None or _snapshot(info) != _snapshot(directories[directory_id]):
                raise DirectoryScrapeConflictError("待清理目录身份已变化，已保留目录")
            assert_guangya_download_delete_allowed(client, info)
            live_protected, _ = _protected_directories()
            report = organizer._clean_empty_dirs_report(
                [(directory_id, 0)], protected_source_ids=protected | live_protected,
            )
            stats["empty_dirs_cleaned"] = stats.get("empty_dirs_cleaned", 0) + report["cleaned"]
            if report.get("reasons"):
                stats.setdefault("directory_cleanup_reasons", []).extend(report["reasons"])
            if any(report.get(key) for key in ("delete_failures", "unsupported", "unavailable")):
                raise DirectoryScrapeConflictError("媒体已回收，但目录清理未完成；请核对删除审计")
            if directory_id in full_roots and not report["cleaned"]:
                raise DirectoryScrapeConflictError("作品目录存在新增或未处理内容，已保留目录")
    except Exception as exc:  # noqa: BLE001 - 未知结果不重放，只退役已确认回收的对象。
        error = public_error_message(exc) if isinstance(exc, DirectoryScrapePublicError) else (
            "云端删除结果未能确认，请在 MF 核对删除审计，不要盲目重试"
            if write_attempted else "删除前校验失败，未执行云端删除，请检查光鸭连接或文件状态"
        )
        logger.warning("Jellyfin 联动删除未完成 type=%s completed=%s", type(exc).__name__, len(completed))
    finally:
        close_guangya_client(client)
    if retired_targets:
        try:
            # 等已在途的扫描/安装完成后，再原子退役；不扫描任何来源目录。
            if not STRM_OPERATION_LOCK.acquire():
                raise RuntimeError("STRM 写锁不可用")
            try:
                stats.update(retire_deleted_strm_files(retired_targets))
            finally:
                STRM_OPERATION_LOCK.release()
        except Exception as exc:
            logger.warning("Jellyfin 删除后索引收尾未完成 type=%s", type(exc).__name__)
            stats["strm_retire_failed"] = 1
            error = error or "云端文件已移入回收站，但 STRM 索引收尾未完成，请在 MF 核对后同步"
    media_ids = {target.file_id for target in expected}
    media_deleted = sum(item["file_id"] in media_ids for item in completed)
    return {"ok": not error, "completed": len(completed), "error": error, "items": completed,
            "stats": {"deleted": len(completed), "media_deleted": media_deleted,
                      "companions_deleted": len(completed) - media_deleted, **stats}}
