"""统一整理的只读目录扫描阶段。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Callable

from app.clients.guangya import GuangYaFile
from app.logger import get_logger
from app.modules.directory_scrape_errors import DirectoryScrapeConflictError
from app.modules.special_media import (
    is_special_media_name,
    is_special_path,
    is_sample_media_path,
    special_parent_context,
)

if TYPE_CHECKING:
    from app.modules.organize import OrganizeContext, OrganizeRules

# 保持历史日志命名，避免扫描拆分改变日志筛选和测试契约。
logger = get_logger("app.modules.organize")

# 防止异常目录图、超深目录树或海量非媒体条目长时间占用统一整理锁。
# 这些是安全护栏而非普通业务筛选；正常媒体库通常远低于该规模。
DEFAULT_TRAVERSAL_MAX_DEPTH = 64
DEFAULT_TRAVERSAL_MAX_DIRS = 50_000
DEFAULT_TRAVERSAL_MAX_ENTRIES = 250_000


def filter_source_entries(
    entries: list[GuangYaFile], source_member_ids: frozenset[str] | None,
) -> list[GuangYaFile]:
    """只约束来源发现，绝不改变真实 client 的目标/写前/清理回读。"""
    if source_member_ids is None:
        return entries
    return [item for item in entries if item.file_id in source_member_ids]


def validate_source_snapshot(
    expected: GuangYaFile, current: GuangYaFile | None, *, role: str,
) -> GuangYaFile:
    """扫描回读和最终写前回读遵守同一对象身份、位置及版本约束。"""
    if (current is None or current.file_id != expected.file_id
            or current.is_dir != expected.is_dir):
        raise DirectoryScrapeConflictError(
            f"{role}已不存在或类型已变化，请刷新目录后重试"
        )
    mismatches = []
    if (current.name or "") != (expected.name or ""):
        mismatches.append("文件名")
    if (current.parent_id or "") != (expected.parent_id or ""):
        mismatches.append("所在目录")
    if not expected.is_dir and (current.size or 0) != (expected.size or 0):
        mismatches.append("文件大小")
    if expected.etag and (current.etag or "") != expected.etag:
        mismatches.append("ETag")
    if mismatches:
        raise DirectoryScrapeConflictError(
            f"{role}在预览后发生变化（{'、'.join(mismatches)}），请刷新目录后重试"
        )
    return current


def record_protected_staging(
    stats: dict, *, source_dir_id: str, file_id: str, path: str,
) -> None:
    """已知保护排除不是读取失败，但不能冒充整源扫描完成。"""
    blocked = stats.setdefault("protected_staging_dirs", [])
    if any(item["source_dir_id"] == source_dir_id and item["file_id"] == file_id
           for item in blocked):
        return
    reason = (
        f"受保护下载目录「{path}」（ID: {file_id}）未扫描："
        "下载隔离目录仍可能被写入或等待核验；已移动目录需核验归属，未自动接管"
    )
    blocked.append({
        "source_dir_id": source_dir_id, "file_id": file_id,
        "path": path, "reason": reason,
    })
    stats["scan_complete"] = False
    reasons = stats.setdefault("skip_reasons", [])
    if reason not in reasons and len(reasons) < 20:
        reasons.append(reason)
    logger.warning("%s", reason)


@dataclass(frozen=True)
class ScanRestriction:
    """把一次扫描限制在单个媒体组子树内。

    ``files_only`` 用于来源根目录直属媒体：只读取根目录本层视频，不下钻，
    避免与第一层媒体目录组重复扫描同一批文件。
    """

    dir_id: str
    rel: str = ""
    files_only: bool = False
    group_id: str = ""
    group_path: str = "__root__"
    etag: str = ""
    updated_at: int = 0
    file_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ScannedVideo:
    file: GuangYaFile
    relative_dir: str
    special: bool = False
    recognition_parent_path: str = ""
    source_group_id: str = ""
    source_group_path: str = ""


@dataclass
class OrganizeScanResult:
    scanned_videos: list[ScannedVideo]
    scanned_dirs: list[tuple[str, int, str, int]]
    companion_files: dict[str, list[GuangYaFile]]
    video_files_by_path: dict[str, list[GuangYaFile]]
    protected_sources: set[str]
    source_root_name: str
    source_entries: dict[str, tuple[GuangYaFile, ...]] = field(default_factory=dict)


class TraversalLimitExceeded(RuntimeError):
    def __init__(self, kind: str, limit: int):
        self.kind = kind
        self.limit = limit
        super().__init__(f"{self.kind}:{self.limit}")


@dataclass
class TraversalBudget:
    max_depth: int
    max_dirs: int
    max_entries: int
    visited: set[str] = field(default_factory=set)
    directories: int = 0
    entries: int = 0
    duplicates: int = 0

    def enter(self, dir_id: str, depth: int) -> bool:
        key = (dir_id or "")
        if key in self.visited:
            self.duplicates += 1
            return False
        if depth > self.max_depth:
            raise TraversalLimitExceeded("depth", self.max_depth)
        if self.directories >= self.max_dirs:
            raise TraversalLimitExceeded("directories", self.max_dirs)
        self.visited.add(key)
        self.directories += 1
        return True

    def consume_entries(self, count: int) -> None:
        value = max(0, (count or 0))
        if self.entries + value > self.max_entries:
            raise TraversalLimitExceeded("entries", self.max_entries)
        self.entries += value


@dataclass
class _ScanAccumulator:
    stats: dict
    scanned_videos: list[ScannedVideo]
    scanned_dirs: list[tuple[str, int, str, int]]
    companion_files: dict[str, list[GuangYaFile]]
    video_files_by_path: dict[str, list[GuangYaFile]]
    protected_sources: set[str]
    video_exts: set[str]
    metadata_exts: set[str]
    source_root_name: str
    traversal: TraversalBudget
    snapshots_by_parent: dict[str, list[GuangYaFile]] = field(default_factory=dict)
    source_entries: dict[str, tuple[GuangYaFile, ...]] = field(default_factory=dict)


class OrganizerScanner:
    """只读扫描来源目录，生成后续识别与执行阶段使用的稳定快照。"""

    def __init__(
        self,
        client,
        *,
        traversal_limits: tuple[int, int, int],
        append_reason: Callable[[dict, str, str], None],
    ) -> None:
        self.client = client
        self.traversal_limits = traversal_limits
        self.append_reason = append_reason

    def scan(
        self,
        context: "OrganizeContext",
        rules: "OrganizeRules",
        stats: dict,
        *,
        video_exts: set[str],
        metadata_exts: set[str],
        restriction: ScanRestriction | None = None,
    ) -> OrganizeScanResult:
        started = time.monotonic()
        protected_sources = set(context.protected_source_ids) | set(context.protected_staging_ids)
        protected_sources.discard(context.source_dir_id)
        source_root_name = context.source_name.strip()
        if context.source_entries is None:
            source_info = None
            try:
                stats["scan_file_info_calls"] += 1
                source_info = self.client.file_info(context.source_dir_id)
                if source_info is not None and getattr(source_info, "name", ""):
                    source_root_name = str(source_info.name).strip()
            except Exception:
                pass
            expected_root = (context.source_snapshots or {}).get(context.source_dir_id)
            if expected_root is not None:
                validate_source_snapshot(expected_root, source_info, role="待确认目录")

        accumulator = _ScanAccumulator(
            stats=stats,
            scanned_videos=[],
            scanned_dirs=[],
            companion_files={},
            video_files_by_path={},
            protected_sources=protected_sources,
            video_exts=video_exts,
            metadata_exts=metadata_exts,
            source_root_name=source_root_name,
            traversal=TraversalBudget(*self.traversal_limits),
        )
        for item in (context.source_snapshots or {}).values():
            if item.file_id == context.source_dir_id or (
                restriction is not None and item.file_id == restriction.dir_id
            ):
                continue
            accumulator.snapshots_by_parent.setdefault(item.parent_id, []).append(item)
        try:
            if restriction is None:
                self._scan_directory(
                    context, rules, accumulator, context.source_dir_id, "", 0,
                    source_group_id=context.source_dir_id,
                    source_group_path="__root__",
                )
            elif restriction.files_only:
                self._scan_directory(
                    context, rules, accumulator, restriction.dir_id,
                    restriction.rel, 0,
                    source_group_id=restriction.group_id or restriction.dir_id,
                    source_group_path=restriction.group_path,
                    files_only=True,
                    selected_file_ids=restriction.file_ids,
                )
            else:
                # 组根目录由枚举阶段发现，父级递归不会再登记它；这里显式
                # 记录版本快照，保证组内整理后仍可安全清理空的组目录。
                if self._scan_directory(
                    context, rules, accumulator, restriction.dir_id,
                    restriction.rel, 1,
                    source_group_id=restriction.group_id or restriction.dir_id,
                    source_group_path=restriction.group_path,
                ):
                    accumulator.scanned_dirs.append((
                        restriction.dir_id,
                        1,
                        (restriction.etag or ""),
                        max(0, (restriction.updated_at or 0)),
                    ))
        except TraversalLimitExceeded as exc:
            labels = {
                "depth": "目录深度",
                "directories": "目录数量",
                "entries": "目录条目数量",
            }
            stats["scan_complete"] = False
            stats["scan_limited"] = 1
            stats["scan_limit_kind"] = exc.kind
            stats["scan_errors"].append(
                f"{labels.get(exc.kind, '目录扫描')}超过安全上限 {exc.limit}，已停止继续扫描"
            )
            logger.warning(
                "整理目录扫描触发安全预算 kind=%s limit=%s dirs=%s entries=%s",
                exc.kind,
                exc.limit,
                accumulator.traversal.directories,
                accumulator.traversal.entries,
            )
        stats["scan_dirs"] = accumulator.traversal.directories
        stats["scan_entries"] = accumulator.traversal.entries
        stats["scan_duplicate_dirs"] = accumulator.traversal.duplicates
        stats["scan_elapsed_seconds"] = round(time.monotonic() - started, 3)
        if accumulator.snapshots_by_parent and not context.cancelled():
            raise DirectoryScrapeConflictError("待确认来源集合已变化，请刷新目录后重试")
        return OrganizeScanResult(
            scanned_videos=accumulator.scanned_videos,
            scanned_dirs=accumulator.scanned_dirs,
            companion_files=accumulator.companion_files,
            video_files_by_path=accumulator.video_files_by_path,
            protected_sources=accumulator.protected_sources,
            source_root_name=accumulator.source_root_name,
            source_entries=accumulator.source_entries,
        )

    def _max_files_reached(
        self, context: "OrganizeContext", stats: dict,
    ) -> bool:
        limit = (context.max_files or 0)
        if not limit or int(stats.get("total") or 0) < limit:
            return False
        # 负数保留旧版“立即停止但不报错”的兼容语义；正数代表扫描
        # 结果被截断，必须显式标记，防止未来误把部分快照用于云盘写入。
        if limit > 0 and not stats.get("scan_limited"):
            stats["scan_complete"] = False
            stats["scan_limited"] = 1
            stats["scan_limit_kind"] = "max_files"
            stats["scan_errors"].append(
                f"视频数量达到预览上限 {limit}，扫描结果仅包含部分文件"
            )
        return True

    def _scan_directory(
        self,
        context: "OrganizeContext",
        rules: "OrganizeRules",
        accumulator: _ScanAccumulator,
        dir_id: str,
        rel: str,
        depth: int,
        *,
        source_group_id: str,
        source_group_path: str,
        files_only: bool = False,
        selected_file_ids: frozenset[str] = frozenset(),
    ) -> bool:
        stats = accumulator.stats
        if context.cancelled():
            stats["stopped"] = 1
            return False
        if self._max_files_reached(context, stats):
            return False
        if not accumulator.traversal.enter(dir_id, depth):
            return False
        try:
            if context.source_entries is None:
                stats["scan_list_dir_calls"] += 1
                files = self.client.list_dir(dir_id)
            else:
                files = list(context.source_entries[dir_id])
            accumulator.traversal.consume_entries(len(files))
            files = filter_source_entries(files, context.source_member_ids)
        except TraversalLimitExceeded:
            raise
        except Exception as exc:
            logger.error("列目录失败 dir_id=%s type=%s", dir_id, type(exc).__name__)
            stats["scan_errors"].append(f"{dir_id}: 目录读取失败")
            return False

        if context.source_snapshots is not None and not context.cancelled():
            current_by_id = {item.file_id: replace(item) for item in files}
            for expected in accumulator.snapshots_by_parent.pop(dir_id, []):
                validate_source_snapshot(
                    expected, current_by_id.get(expected.file_id), role="待确认来源",
                )
                current_by_id[expected.file_id] = replace(expected)
            # 成员范围与版本约束独立：允许目录只有成员 ID，文件仍保持原确认版本。
            files = list(current_by_id.values())
            accumulator.source_entries[dir_id] = tuple(files)

        accumulator.video_files_by_path[rel] = [
            item
            for item in files
            if not item.is_dir
            and (
                item.name.rsplit(".", 1)[-1].lower() if "." in item.name else ""
            )
            in accumulator.video_exts
            and (
                not selected_file_ids
                or str(getattr(item, "file_id", "") or "") in selected_file_ids
            )
        ]
        metadata_here = [
            item
            for item in files
            if not item.is_dir
            and (
                item.name.rsplit(".", 1)[-1].lower() if "." in item.name else ""
            )
            in accumulator.metadata_exts
        ]
        if metadata_here:
            accumulator.companion_files[rel] = metadata_here

        for item in files:
            if context.cancelled():
                stats["stopped"] = 1
                return False
            if self._max_files_reached(context, stats):
                return False
            if item.is_dir:
                if files_only:
                    continue
                child_id = (item.file_id or "").strip()
                if not child_id or child_id in accumulator.protected_sources:
                    if child_id in context.protected_staging_ids:
                        sub = f"{rel}/{item.name}" if rel else item.name
                        record_protected_staging(
                            stats, source_dir_id=context.source_dir_id,
                            file_id=child_id,
                            path=f"{accumulator.source_root_name}/{sub}".strip("/"),
                        )
                    continue
                sub = f"{rel}/{item.name}" if rel else item.name
                child_group_id = child_id if depth == 0 else source_group_id
                child_group_path = sub if depth == 0 else source_group_path
                if self._scan_directory(
                    context, rules, accumulator, child_id, sub, depth + 1,
                    source_group_id=child_group_id,
                    source_group_path=child_group_path,
                ):
                    try:
                        updated_at = max(
                            0, int(getattr(item, "updated_at", 0) or 0)
                        )
                    except (TypeError, ValueError):
                        updated_at = 0
                    accumulator.scanned_dirs.append(
                        (
                            child_id,
                            depth + 1,
                            str(getattr(item, "etag", "") or ""),
                            updated_at,
                        )
                    )
                continue
            ext = item.name.rsplit(".", 1)[-1].lower() if "." in item.name else ""
            if ext not in accumulator.video_exts:
                continue
            if (
                selected_file_ids
                and str(getattr(item, "file_id", "") or "") not in selected_file_ids
            ):
                continue
            media_path = f"{rel}/{item.name}" if rel else item.name
            if is_sample_media_path(media_path):
                stats["skipped"] += 1
                self.append_reason(
                    stats,
                    "skip_reasons",
                    "疑似 sample/proof 样片，已保留且不自动归档",
                )
                continue
            if (
                rules.small_file_mb
                and 0 < item.size < rules.small_file_mb * 1024 * 1024
            ):
                stats["skipped"] += 1
                self.append_reason(stats, "skip_reasons", "低于小文件阈值")
                continue
            stats["total"] += 1
            special = is_special_path(rel) or is_special_media_name(item.name)
            accumulator.scanned_videos.append(
                ScannedVideo(
                    file=item,
                    relative_dir=rel,
                    special=special,
                    recognition_parent_path=(
                        special_parent_context(rel, accumulator.source_root_name)
                        if special
                        else rel
                    ),
                    source_group_id=source_group_id,
                    source_group_path=source_group_path,
                )
            )
        return True
