"""统一整理的只读目录扫描阶段。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from app.clients.guangya import GuangYaFile
from app.logger import get_logger
from app.modules.special_media import (
    is_special_media_name,
    is_special_path,
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


class TraversalLimitExceeded(RuntimeError):
    def __init__(self, kind: str, limit: int):
        self.kind = str(kind)
        self.limit = int(limit)
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
        key = str(dir_id or "")
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
        value = max(0, int(count or 0))
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
        protected_sources = set(context.protected_source_ids)
        protected_sources.discard(context.source_dir_id)
        source_root_name = context.source_name.strip()
        try:
            stats["scan_file_info_calls"] += 1
            source_info = self.client.file_info(context.source_dir_id)
            if source_info is not None and getattr(source_info, "name", ""):
                source_root_name = str(source_info.name).strip()
        except Exception:
            pass

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
                        str(restriction.dir_id),
                        1,
                        str(restriction.etag or ""),
                        max(0, int(restriction.updated_at or 0)),
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
        return OrganizeScanResult(
            scanned_videos=accumulator.scanned_videos,
            scanned_dirs=accumulator.scanned_dirs,
            companion_files=accumulator.companion_files,
            video_files_by_path=accumulator.video_files_by_path,
            protected_sources=accumulator.protected_sources,
            source_root_name=accumulator.source_root_name,
        )

    def _max_files_reached(
        self, context: "OrganizeContext", stats: dict,
    ) -> bool:
        limit = int(context.max_files or 0)
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
            stats["scan_list_dir_calls"] += 1
            files = self.client.list_dir(dir_id)
            accumulator.traversal.consume_entries(len(files))
        except TraversalLimitExceeded:
            raise
        except Exception as exc:
            logger.error("列目录失败 dir_id=%s type=%s", dir_id, type(exc).__name__)
            stats["scan_errors"].append(f"{dir_id}: 目录读取失败")
            return False

        accumulator.video_files_by_path[rel] = [
            item
            for item in files
            if not item.is_dir
            and (
                item.name.rsplit(".", 1)[-1].lower() if "." in item.name else ""
            )
            in accumulator.video_exts
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
                child_id = str(item.file_id or "").strip()
                if not child_id or child_id in accumulator.protected_sources:
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


# 保留 organize.py 历史私有名称，减少现有类型注解与调试工具的迁移成本。
_ScannedVideo = ScannedVideo
_TraversalLimitExceeded = TraversalLimitExceeded
_TraversalBudget = TraversalBudget
