"""STRM 迁移凭据恢复与历史副本校准；复用 STRM 的所有权和变化交接逻辑。"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path

from app import database as db
from app.repositories import strm as strm_repo
from app.modules.media_refresh import plan_refresh_targets

# 只读小型指针文件，不把伪装成 .strm 的媒体文件加载进内存。
MAX_POINTER_BYTES = 16 * 1024


def _blocked(stats: dict, path: object, message: str) -> None:
    from app.modules import strm

    stats["clean_skipped"] = True
    stats["recovery_pending"] = int(stats.get("recovery_pending", 0)) + 1
    strm._append_error_sample(stats, "恢复 STRM 清理", path, RuntimeError(message))


def _stopped(stats: dict, should_stop: Callable[[], bool] | None) -> bool:
    if stats.get("stopped") or (should_stop and should_stop()):
        stats.update(stopped=True, stop_stage="recovery-cleanup", clean_skipped=True)
        return True
    return False


def _managed_path(value: object, strm_root: str) -> Path | None:
    """限定光鸭输出子树；不把索引或扫描时出现的符号链接解析成删除目标。"""
    raw = Path(str(value or "")).expanduser()
    if not str(value or "").strip():
        return None
    if not raw.is_absolute():
        raw = Path(strm_root).expanduser() / raw
    try:
        from app.modules.strm import STRM_SUBDIR

        resolved = raw.resolve(strict=False)
        resolved.relative_to(
            (Path(strm_root).expanduser() / STRM_SUBDIR).resolve(strict=False)
        )
        if raw.absolute() != resolved or raw.is_symlink():
            return None
        return resolved
    except (OSError, ValueError, RuntimeError):
        return None


def _cleanup_roots(source_key: str, strm_root: str) -> dict[Path, Path]:
    """按顺序保留登记根与物理路径，供绝对凭据核验。"""
    provider, _, source_id = source_key.partition(":")
    recorded = (
        db.get_strm_source_output_roots(source_id)
        if provider in {"guangya", "guangya-meta"} and source_id else []
    )
    configured = Path(os.path.abspath(os.path.expanduser(strm_root))) if strm_root else None
    declared = ([configured] if configured is not None else []) + [Path(root) for root in recorded]
    roots: dict[Path, Path] = {}
    for root in dict.fromkeys(declared):
        try:
            physical = root.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            physical = root
        # 当前配置显式授权物理根；历史别名只与原绝对凭据交叉核验。
        roots[physical if root == configured else root] = physical
    return roots


def _cleanup_location(value: object, roots: dict[Path, Path]) -> tuple[Path, Path] | None:
    """绝对凭据必须指向登记根的真实子树；相对凭据仅适用唯一且明确的根。"""
    from app.modules.strm import STRM_SUBDIR

    if not value:
        return None
    try:
        raw = Path(str(value).strip()).expanduser()
    except RuntimeError:
        return None
    if raw.is_absolute():
        # 历史根改指时，索引中原来的物理路径不会落入新目标。删除仍复核文件指纹。
        root = max(
            (physical for physical in roots.values() if raw.is_relative_to(physical / STRM_SUBDIR)),
            key=lambda root: len(root.parts), default=None,
        )
    elif len(roots) == 1:
        registered, root = next(iter(roots.items()))
        if registered != root:
            return None
    else:
        return None
    if root is not None:
        subtree = root / STRM_SUBDIR
        path = _managed_path(value, str(root))
        if path is not None and path != subtree and path.is_relative_to(subtree):
            return root, path
    return None


def _source_dir_path(media_root: Path, rel_prefix: str) -> Path:
    """按 STRM 生成规则解析来源目录。"""
    from app.modules import strm

    return media_root / strm.source_directory_component(rel_prefix)


def _protected_adoption_dirs(
    media_root: Path, sources: list[dict[str, str]] | None = None,
) -> set[Path]:
    """返回尚未接管的旧目录，供各清理路径保护同一批候选。"""
    from app.modules import strm

    if sources is None:
        sources, _ = strm.configured_strm_source_plans()
    strm_root = str(media_root.parent)
    return {
        _source_dir_path(media_root, prefix)
        for source in sources
        if (prefix := db.get_strm_adoption_prefix(
            source["id"], strm_root, source.get("rel_prefix", ""),
        ))
    }


def recover_pending_paths(
    source_key: str,
    strm_root: str,
    stats: dict,
    *,
    valid_ids: set[str],
    only_file_ids: set[str] | None = None,
    should_stop: Callable[[], bool] | None = None,
    on_refresh_paths: Callable[[list[str]], object] | None = None,
) -> bool:
    """索引迁移与历史副本统一消费旧路径凭据，绝不删除仍被索引引用的路径。

    valid_ids 是本轮确认仍有效的 ID；精准增量必须同时给 only_file_ids，
    避免把未核查的其他 ID 当成远端删除。当前新文件存在且指纹正确才删旧
    副本；已确认远端删除的 ID 可以直接使用原索引指纹清理。
    本范围内的凭据均已确认或本来为空时返回 True。
    """
    from app.modules import strm

    roots: dict[Path, Path] | None = None
    protected_dirs: set[Path] = set()
    current = None
    reported_paths = set(stats.get("changed_strm_paths") or [])
    after_id = 0
    pending_receipts = 0
    while True:
        batch = strm_repo.list_strm_path_cleanup(
            source_key, after_id=after_id, only_file_ids=only_file_ids,
        )
        pending_receipts += len(batch)
        if _stopped(stats, should_stop) or not batch:
            return pending_receipts == 0
        after_id = int(batch[-1]["id"])
        if roots is None:
            roots = _cleanup_roots(source_key, strm_root)
            if strm_root:
                media_root = Path(strm_root).expanduser().resolve(strict=False) / strm.STRM_SUBDIR
                protected_dirs = _protected_adoption_dirs(media_root)
        by_root: dict[Path, list[tuple[Path, sqlite3.Row]]] = {}
        for row in batch:
            location = _cleanup_location(row["strm_path"], roots)
            if location is None:
                _blocked(
                    stats, row["strm_path"], "无法核实旧路径的 STRM 输出根或路径经过链接，已保留清理凭据"
                )
                continue
            root, path = location
            if protected_dirs and any(path.is_relative_to(item) for item in protected_dirs):
                continue
            by_root.setdefault(root, []).append((path, row))
        for root in dict.fromkeys(roots.values()):
            items = by_root.get(root)
            if not items:
                continue
            media_root = root / strm.STRM_SUBDIR
            sink = on_refresh_paths
            if isinstance(sink, strm_repo.StrmRefreshPolicy) and sink.media_root != str(media_root):
                sink = replace(sink, media_root=str(media_root))
            retained = strm._retained_indexed_paths((str(path) for path, _row in items), str(root))
            completed: list[int] = []
            changed_paths: list[str] = []
            for path, row in items:
                if _stopped(stats, should_stop):
                    break
                file_id = str(row["file_id"])
                try:
                    if str(path) in retained:
                        # 普通异常已回滚，或路径已被其他活跃对象复用；凭据不再适用。
                        completed.append(int(row["id"]))
                        continue
                    if path.exists() and file_id in valid_ids:
                        if current is None:
                            current = {
                                str(item["file_id"]): item
                                for item in strm_repo.list_strm_index(source_key, only_file_ids=only_file_ids)
                            }
                        active = current.get(file_id)
                        if (
                            active is None
                            or (replacement := _cleanup_location(active["strm_path"], roots)) is None
                            or not strm._fingerprint_matches(replacement[1], active["content_fingerprint"])
                        ):
                            _blocked(stats, path, "新文件尚未验证，已保留旧 STRM 和清理凭据")
                            continue
                    if (not media_root.is_dir() or _managed_path(path, str(root)) != path
                            or (path.exists() and not path.is_file())):
                        _blocked(stats, path, "旧输出根无法访问，或旧路径已变为链接或非普通文件，需人工核对")
                        continue
                    deleted = strm._delete_owned_file(path, [row], "恢复旧 STRM 清理")
                    stats["metadata_cleaned" if source_key.startswith("guangya-meta:") else "cleaned"] += int(deleted)
                    # 已删但未确认的凭据同样重放路径变化，防止中断后媒体库漏刷新。
                    if deleted or str(path) not in reported_paths:
                        strm._track_change(stats, "removed", path, root, on_refresh_paths=sink)
                        if len(reported_paths) < strm._MAX_TRACKED_CHANGED_PATHS:
                            reported_paths.add(str(path))
                    changed_paths.append(str(path))
                    if _stopped(stats, should_stop):
                        break
                    completed.append(int(row["id"]))
                except (OSError, RuntimeError, ValueError) as exc:
                    _blocked(stats, path, str(exc))
            # 刷新持久化和凭据确认都可重放，失败时保留凭据供下一轮继续交接。
            if completed:
                changed_targets = plan_refresh_targets(changed_paths, media_roots=[str(media_root)]).targets
                try:
                    if changed_targets and sink:
                        sink(list(changed_targets))
                    strm_repo.delete_strm_path_cleanup(completed)
                    pending_receipts -= len(completed)
                except Exception as exc:
                    _blocked(stats, source_key, f"旧路径刷新或凭据确认失败（{type(exc).__name__}），保留清理凭据")
                    stats.update(stopped=True, stop_stage="refresh-persist")
                    return False


def _pointer(path: Path) -> bytes:
    with path.open("rb") as handle:
        return handle.read(MAX_POINTER_BYTES + 1)


def _local_pointers(
    roots: list[Path], stats: dict, should_stop, protected_dirs: set[Path],
) -> Iterator[Path]:
    """每轮只遍历一次所属目录；条目、目录数、时间有界，不跟随符号链接。"""
    from app.modules import strm

    max_dirs, max_entries, _candidates, seconds = strm._scan_limits()
    deadline = time.monotonic() + seconds
    pending = deque(root for root in roots if root not in protected_dirs)
    visited: set[Path] = set()
    entries = 0
    while pending and not _stopped(stats, should_stop):
        directory = pending.popleft()
        if directory in visited:
            continue
        if directory.is_symlink():
            continue
        if len(visited) >= max_dirs or time.monotonic() >= deadline:
            _blocked(
                stats, directory, "历史 STRM 校准达到目录或时间上限，未检查项已保留"
            )
            return
        visited.add(directory)
        if not directory.exists():
            continue
        try:
            with os.scandir(directory) as children:
                for entry in children:
                    if _stopped(stats, should_stop):
                        return
                    if entries >= max_entries or time.monotonic() >= deadline:
                        _blocked(
                            stats,
                            directory,
                            "历史 STRM 校准达到条目或时间上限，未检查项已保留",
                        )
                        return
                    entries += 1
                    if entry.is_dir(follow_symlinks=False):
                        child = Path(entry.path)
                        if child in protected_dirs:
                            continue
                        if len(pending) + len(visited) >= max_dirs:
                            _blocked(
                                stats,
                                directory,
                                "历史 STRM 校准达到目录上限，未检查项已保留",
                            )
                            return
                        pending.append(child)
                    elif entry.is_file(
                        follow_symlinks=False
                    ) and entry.name.lower().endswith(".strm"):
                        yield Path(entry.path)
        except OSError as exc:
            _blocked(
                stats, directory, f"历史 STRM 目录读取失败（{type(exc).__name__}）"
            )


def reconcile_historical_strm(
    strm_root: str,
    base_url: str,
    sources: list[dict[str, str]],
    stats: dict,
    *,
    all_sources: bool = False,
    should_stop: Callable[[], bool] | None = None,
    on_refresh_paths: Callable[[list[str]], object] | None = None,
) -> None:
    """只在完整扫描和索引清理均成功后校准，绝不用于快速同步。

    旧副本必须与本轮有效索引、实际安装内容、当前签名播放 URL 三者精确
    一致。其他来源的索引路径、手工内容、第三方指针和伴随文件一律保留。
    """
    from app.modules import strm

    if stats.get("clean_skipped") or _stopped(stats, should_stop):
        return
    root = Path(strm_root).expanduser().resolve(strict=False) / strm.STRM_SUBDIR
    if not sources or not root.is_dir() or root.is_symlink():
        return
    source_keys = {f"guangya:{source['id']}" for source in sources}
    rows = strm_repo.list_strm_index_by_prefix("")
    indexed_paths: set[Path] = set()
    replacements: dict[bytes, list[tuple[str, str, Path, str]]] = {}
    valid_ids_by_source: dict[str, set[str]] = {}
    for row in rows:
        if _stopped(stats, should_stop):
            return
        path = strm._safe_indexed_path(row["strm_path"], strm_root)
        if not path:
            continue
        indexed_paths.add(path)
        if _managed_path(row["strm_path"], strm_root) is None:
            continue
        if str(row["source"]) not in source_keys or path.suffix.lower() != ".strm":
            continue
        try:
            url = strm.build_play_url(
                base_url, row["file_id"], row["etag"], row["size"], row["filename"]
            )
            payload = url.encode("utf-8")
            fingerprint = f"sha256:{hashlib.sha256(payload).hexdigest()}"
            if (
                len(payload) <= MAX_POINTER_BYTES
                and row["content_fingerprint"] == fingerprint
            ):
                # 这里只整理索引与签名 URL 的候选关系；遇到实际旧副本时
                # 才在删除边界校验新文件，避免每次全量校准重读所有有效指针。
                source_key, file_id = str(row["source"]), str(row["file_id"])
                replacements.setdefault(payload, []).append((source_key, file_id, path, fingerprint))
                valid_ids_by_source.setdefault(source_key, set()).add(file_id)
        except (OSError, ValueError, TypeError):
            continue
    if all_sources:
        roots = [root]
    else:
        roots = list(dict.fromkeys(
            _source_dir_path(root, source.get("rel_prefix") or "") for source in sources
        ))
    pending: list[dict[str, str]] = []

    def persist_pending() -> bool:
        if not pending:
            return True
        try:
            strm_repo.enqueue_strm_path_cleanup(pending)
        except (sqlite3.Error, RuntimeError, ValueError) as exc:
            _blocked(stats, root, f"历史 STRM 清理凭据持久化失败（{type(exc).__name__}）")
            stats.update(stopped=True, stop_stage="recovery-persist")
            return False
        pending.clear()
        return True

    protected_dirs = _protected_adoption_dirs(root, sources)
    for candidate in _local_pointers(roots, stats, should_stop, protected_dirs):
        try:
            path = _managed_path(candidate, strm_root)
            if path is None:
                _blocked(stats, candidate, "历史 STRM 路径已变化或越界，已保留")
                continue
            if path in indexed_paths or candidate.is_symlink():
                continue
            payload = _pointer(path)
            matches = replacements.get(payload, [])
            if not matches:
                # 只提示形似本项目的失联指针；其他工具生成的指针不算同步失败。
                if b"/playgy/" in payload:
                    _blocked(
                        stats,
                        path,
                        "历史 STRM 无索引且无法验证有效新副本，已保留，请人工核对",
                    )
                continue
            # 选择真正存在的替代项以确定持久凭据所属来源；实际删除前由
            # 统一消费者再次复核新副本与全部路径所有者，不能只信此处快照。
            replacement = next((
                item for item in matches if strm._fingerprint_matches(item[2], item[3])
            ), None)
            if replacement is None:
                _blocked(stats, path, "历史 STRM 的有效新副本已变化，已保留旧文件")
                continue
            source_key, file_id, _new, fingerprint = replacement
            pending.append({
                "source": source_key,
                "file_id": file_id,
                "strm_path": str(path),
                "content_fingerprint": fingerprint,
            })
            if len(pending) >= 500 and not persist_pending():
                return
        except (OSError, RuntimeError, ValueError) as exc:
            _blocked(stats, candidate, str(exc))

    if _stopped(stats, should_stop) or not persist_pending():
        return
    for source_key, valid_ids in sorted(valid_ids_by_source.items()):
        if _stopped(stats, should_stop):
            return
        # 同时恢复前次已删除但尚未交接刷新的凭据，即使磁盘上已没有旧副本。
        # 这里只处理当前有可信索引的ID，不把未核查的其它ID当成远端删除。
        recover_pending_paths(
            source_key, strm_root, stats, valid_ids=valid_ids, only_file_ids=valid_ids,
            should_stop=should_stop, on_refresh_paths=on_refresh_paths,
        )
