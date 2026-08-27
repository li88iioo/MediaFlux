"""STRM 变化路径到媒体库刷新目标的聚合。

目标是把本轮真实变化的 STRM 路径收敛成最小但稳定的刷新目录集合，
避免每次整理都对整个 ``STRM_ROOT`` 或全局媒体库做全量刷新。

规则：
- 同一目录内的多个文件只产生一个刷新目标。
- 祖先目录已入选时，其后代目录不再单独刷新。
- 不提前把不同作品的兄弟目录收敛为库根；最终由媒体服务器 Item ID 去重。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from app.logger import get_logger

logger = get_logger(__name__)

# 单轮刷新目标上限，防止异常变化集把刷新放大成海量请求。
MAX_REFRESH_TARGETS = 200


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

    @property
    def has_targets(self) -> bool:
        return bool(self.targets)

    @property
    def batches(self) -> tuple[tuple[str, ...], ...]:
        """按单次媒体服务器请求上限切分，但不丢弃任何刷新目标。"""
        size = max(1, int(self.batch_size or MAX_REFRESH_TARGETS))
        return tuple(
            self.targets[offset:offset + size]
            for offset in range(0, len(self.targets), size)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "targets": list(self.targets),
            "omitted": int(self.omitted),
            "reason": self.reason,
            "batch_size": int(self.batch_size),
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
    changed_paths: object = (),
    changed_dirs: object = (),
    *,
    media_roots: object = (),
    max_targets: int = MAX_REFRESH_TARGETS,
) -> RefreshPlan:
    """把变化文件与目录收敛成最小刷新目标集合。"""
    boundary = _Boundary(tuple(
        item for item in (_normalize(root) for root in media_roots or ()) if item
    ))
    candidates: list[str] = []
    for item in changed_dirs or ():
        normalized = _normalize(item)
        if normalized:
            candidates.append(normalized)
    for item in changed_paths or ():
        normalized = _normalize(item)
        if not normalized:
            continue
        parent = str(PurePosixPath(normalized).parent)
        if parent and parent not in {".", "/"}:
            candidates.append(parent)
    unique = [item for item in dict.fromkeys(candidates) if boundary.allows(item)]
    if not unique:
        return RefreshPlan(reason="本轮没有可定位的变化目录")

    # 兄弟目录可能是不同电影/剧集；过早折叠会把本可精准刷新的两个作品
    # 放大成物理根扫描。相同 Series 的多季变化由客户端解析 Item ID 后去重。
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
    ordered = sorted(dict.fromkeys(paths), key=len)
    kept: list[str] = []
    for path in ordered:
        if any(_is_descendant(path, existing) for existing in kept):
            continue
        kept.append(path)
    return kept
