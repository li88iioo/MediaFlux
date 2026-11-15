"""STRM 变化路径到媒体库刷新目标的聚合。

目标是把本轮真实变化的 STRM 路径收敛成最小但稳定的刷新目录集合，
避免每次整理都对整个 ``STRM_ROOT`` 或全局媒体库做全量刷新。

规则：
- 目录内只有一个变化文件时保留文件路径，供客户端定位对应作品/电影。
- 同一目录多个变化文件收敛为该目录，让整批改写/删除的交接量按目录增长；
  客户端再把同剧的各季合并为一次作品刷新，同时更新整剧最新单集日期。
- 祖先目录已入选时，其后代不再单独刷新；不把兄弟目录收敛为库根。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Iterable

from app.logger import get_logger

logger = get_logger(__name__)

# 单轮刷新目标上限，防止异常变化集把刷新放大成海量请求。
MAX_REFRESH_TARGETS = 200


def _batch_size(value: int) -> int:
    return max(1, (value or MAX_REFRESH_TARGETS))


def _normalize(path: object) -> str:
    text = str(path or "").replace("\\", "/").rstrip("/")
    return text


def _is_descendant(candidate: str, ancestor: str) -> bool:
    if not candidate or not ancestor:
        return False
    return candidate == ancestor or candidate.startswith(f"{ancestor}/")


@dataclass(frozen=True)
class RefreshPlan:
    """一轮精准刷新的目标集合与降级说明。"""

    targets: tuple[str, ...] = ()
    omitted: int = 0
    reason: str = ""
    batch_size: int = MAX_REFRESH_TARGETS

    def __post_init__(self) -> None:
        object.__setattr__(self, "batch_size", _batch_size(self.batch_size))

    @property
    def has_targets(self) -> bool:
        return bool(self.targets)

    @property
    def batches(self) -> tuple[tuple[str, ...], ...]:
        """按单次媒体服务器请求上限切分，但不丢弃任何刷新目标。"""
        size = self.batch_size
        return tuple(
            self.targets[offset:offset + size]
            for offset in range(0, len(self.targets), size)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "targets": list(self.targets),
            "omitted": self.omitted,
            "reason": self.reason,
            "batch_size": self.batch_size,
        }


@dataclass
class _Boundary:
    """刷新目标不得收敛到媒体根之上，否则等价于全库刷新。"""

    roots: tuple[str, ...] = field(default_factory=tuple)

    def allows(self, path: str) -> bool:
        if not self.roots:
            return True
        return any(
            _is_descendant(path, root) and path != root for root in self.roots
        )


def plan_refresh_targets(
    changed_paths: Iterable[object] | None = (),
    changed_dirs: Iterable[object] | None = (),
    *,
    media_roots: Iterable[object] | None = (),
    max_targets: int = MAX_REFRESH_TARGETS,
) -> RefreshPlan:
    """把变化文件与溢出目录收敛成最小刷新目标集合。"""
    max_targets = _batch_size(max_targets)
    boundary = _Boundary(tuple(
        item for item in (_normalize(root) for root in media_roots or ()) if item
    ))
    candidates: list[str] = []
    for item in changed_dirs or ():
        normalized = _normalize(item)
        if normalized:
            candidates.append(normalized)
    files_by_dir: dict[str, list[str]] = {}
    for item in changed_paths or ():
        normalized = _normalize(item)
        if normalized:
            parent = str(PurePosixPath(normalized).parent)
            files_by_dir.setdefault(parent, []).append(normalized)
    for parent, files in files_by_dir.items():
        files = list(dict.fromkeys(files))
        # 媒体根本身不能成为目标；根下直放的文件只能逐个精确定位。
        candidates.extend([parent] if len(files) > 1 and boundary.allows(parent) else files)
    unique = [item for item in dict.fromkeys(candidates) if boundary.allows(item)]
    if not unique:
        return RefreshPlan(reason="本轮没有可定位的变化目录")

    # 兄弟目录可能是不同作品；过早折叠会把本可精准刷新的目标放大成物理根
    # 扫描。整剧/物理根的取舍由客户端解析 Item 后统一决定。
    compressed = _drop_descendants(unique)
    ordered = sorted(compressed)
    if len(ordered) <= max_targets:
        return RefreshPlan(targets=tuple(ordered), batch_size=max_targets)
    batch_count = (len(ordered) + max_targets - 1) // max_targets
    logger.info(
        "媒体库刷新目标超过单批上限，将分批执行 total=%s batch_size=%s batches=%s",
        len(ordered), max_targets, batch_count,
    )
    return RefreshPlan(
        targets=tuple(ordered),
        omitted=0,
        reason=f"刷新目标较多，将分 {batch_count} 批执行",
        batch_size=max_targets,
    )


def _drop_descendants(paths: list[str]) -> list[str]:
    """祖先已入选时丢弃其后代，保证同一子树只刷新一次。"""
    # 以目录分隔符结尾排序，让同一子树连续，且父目录先于子目录。
    # 不能直接按 path 排序后只比最后一项：A-b 会插在 A 与 A/b 之间。
    ordered = sorted(dict.fromkeys(paths), key=lambda path: f"{path}/")
    kept: list[str] = []
    for path in ordered:
        if kept and _is_descendant(path, kept[-1]):
            continue
        kept.append(path)
    return kept
