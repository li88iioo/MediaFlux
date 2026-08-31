"""一次整理任务内共享的只读识别与目标库存运行态。

运行态严格限制在单次整理任务生命周期内：它减少同一任务中的重复网络读取，
不会跨任务复用可能过期的云端目录快照或识别结果。
"""
from __future__ import annotations

import copy
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Hashable


@dataclass
class TargetInventorySnapshot:
    """单写入器维护的目标目录库存。"""

    files: list[Any] = field(default_factory=list)
    evidence_names: dict[str, str] = field(default_factory=dict)
    refreshed_at: float = field(default_factory=time.monotonic)
    writes_since_refresh: int = 0
    # 目标目录自身的轻量版本。光鸭返回 etag/updated_at 时可先校验目录
    # 是否变化，再决定是否重新分页读取完整库存；字段缺失时严格回退刷新。
    revision: tuple[str, int] | None = None
    valid: bool = True


class OrganizeTaskRuntime:
    """跨来源/媒体组共享、但不跨任务持久化的运行态。"""

    def __init__(self) -> None:
        # 云盘写入仍由外层单写门保护；此锁同时兼容预览/测试中的直接调用。
        self.inventory_lock = threading.RLock()
        self.target_inventories: dict[str, TargetInventorySnapshot] = {}
        self.target_path_ids: dict[tuple[str, str], str] = {}
        self.directory_chain_cache: dict[tuple[str, str], str] = {}
        self.target_episode_inventory_cache: dict[tuple[str, int], list[int]] = {}
        self.identity_history_cache: dict[str, set[tuple[str, str]]] = {}

        self._recognition_condition = threading.Condition(threading.RLock())
        self._recognition_results: dict[Hashable, Any] = {}
        self._recognition_inflight: set[Hashable] = set()

    @staticmethod
    def target_path_key(root_id: str, target_path: str) -> tuple[str, str]:
        return str(root_id or ""), str(target_path or "").strip("/")

    def remember_target_path(
        self, root_id: str, target_path: str, target_id: str | None,
    ) -> None:
        key = self.target_path_key(root_id, target_path)
        with self.inventory_lock:
            # 空值不长期缓存：目录可能在稍后的执行阶段被本任务或外部操作创建。
            if target_id:
                self.target_path_ids[key] = str(target_id)
            else:
                self.target_path_ids.pop(key, None)

    def target_id_for_path(self, root_id: str, target_path: str) -> str:
        key = self.target_path_key(root_id, target_path)
        with self.inventory_lock:
            return str(self.target_path_ids.get(key) or "")

    def get_inventory(self, target_id: str) -> TargetInventorySnapshot | None:
        with self.inventory_lock:
            snapshot = self.target_inventories.get(str(target_id or ""))
            if snapshot is None or not snapshot.valid:
                return None
            return snapshot

    @staticmethod
    def inventory_revision(item: Any) -> tuple[str, int] | None:
        if item is None:
            return None
        etag = str(getattr(item, "etag", "") or "").strip()
        try:
            updated_at = max(0, int(getattr(item, "updated_at", 0) or 0))
        except (TypeError, ValueError):
            updated_at = 0
        if not etag and not updated_at:
            return None
        return etag, updated_at

    def store_inventory(
        self,
        target_id: str,
        files: list[Any],
        evidence_names: dict[str, str] | None = None,
        *,
        revision: tuple[str, int] | None = None,
    ) -> TargetInventorySnapshot:
        snapshot = TargetInventorySnapshot(
            files=list(files or []),
            evidence_names=dict(evidence_names or {}),
            revision=revision,
        )
        with self.inventory_lock:
            self.target_inventories[str(target_id or "")] = snapshot
        return snapshot

    def set_inventory_revision(
        self, target_id: str, revision: tuple[str, int] | None,
    ) -> None:
        with self.inventory_lock:
            snapshot = self.target_inventories.get(str(target_id or ""))
            if snapshot is not None:
                snapshot.revision = revision

    def invalidate_inventory(self, target_id: str) -> None:
        with self.inventory_lock:
            snapshot = self.target_inventories.get(str(target_id or ""))
            if snapshot is not None:
                snapshot.valid = False
            for key in [
                item for item in self.target_episode_inventory_cache
                if item[0] == str(target_id or "")
            ]:
                self.target_episode_inventory_cache.pop(key, None)

    def note_inventory_write(self, target_id: str) -> None:
        with self.inventory_lock:
            snapshot = self.target_inventories.get(str(target_id or ""))
            if snapshot is not None:
                snapshot.writes_since_refresh += 1

    def resolve_recognition(
        self,
        key: Hashable,
        loader: Callable[[], Any],
        *,
        cacheable: Callable[[Any], bool],
        neutralize: Callable[[Any], Any],
    ) -> tuple[Any, bool, float, bool]:
        """对严格作品身份执行 single-flight。

        返回 ``(result, cache_hit, wait_seconds, cache_bound)``。首个调用负责
        共享查询；若结果不满足严格缓存条件，所有等待者会各自恢复原识别链路，
        不会被串行排队，也不会扩散低置信、AI 或人工确认结果。
        """
        waited = 0.0
        owner = False
        with self._recognition_condition:
            cached = self._recognition_results.get(key)
            if cached is not None:
                return copy.deepcopy(cached), True, waited, False
            if key not in self._recognition_inflight:
                self._recognition_inflight.add(key)
                owner = True
            else:
                wait_started = time.monotonic()
                while key in self._recognition_inflight:
                    self._recognition_condition.wait()
                waited = max(0.0, time.monotonic() - wait_started)
                cached = self._recognition_results.get(key)
                if cached is not None:
                    return copy.deepcopy(cached), True, waited, False

        # 首个结果不可缓存时，等待者从这里并行恢复完整识别，不再形成
        # “一个失败结果让同组查询逐个串行”的隐性性能退化。
        try:
            result = loader()
            bound = bool(result is not None and cacheable(result))
            cached_result = neutralize(result) if bound else None
            with self._recognition_condition:
                if bound:
                    self._recognition_results.setdefault(
                        key, copy.deepcopy(cached_result),
                    )
                if owner:
                    self._recognition_inflight.discard(key)
                self._recognition_condition.notify_all()
            return result, False, waited, bound
        except BaseException:
            with self._recognition_condition:
                if owner:
                    self._recognition_inflight.discard(key)
                self._recognition_condition.notify_all()
            raise
