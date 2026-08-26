"""qB 完成、定时扫描与稳定等待驱动的本地媒体调度器。"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from app import database as db
from app.clients.qbittorrent import QBittorrentClient, TorrentTask
from app.config import get
from app.logger import get_logger, log_throttled
from app.modules.local_media_service import LocalMediaService
from app.modules.local_media_candidates import discover_local_media_candidates
from app.modules.local_media_notifications import notify_local_media_task
from app.modules.local_path_mapping import (
    LEGACY_SOURCE_PATH_ERROR,
    PathMapping,
    PathMappingError,
    assert_within,
    require_container_absolute_path,
)
from app.modules.local_storage import LocalFilesystemAdapter, LocalStorageError, snapshot_digest

logger = get_logger(__name__)


MANUAL_SCAN_TOKEN_PREFIX = "manual-scan:"
SILENT_MANUAL_SCAN_TOKEN_PREFIX = "silent-manual-scan:"
_MAX_CAPTURED_TASK_RESULTS = 1000


class LocalMediaProbeRetryable(RuntimeError):
    """qB 完成内容暂时无法可靠判断，调用方应保留任务并稍后重试。"""


class LocalMediaSourceMigrationRequired(RuntimeError):
    """qB 已命中遗留来源，但该来源必须迁移为 Docker 容器路径。"""


def _source_path_error(source) -> str:
    try:
        root = require_container_absolute_path(source.local_root, label="来源目录")
        assert_within(root, root)
    except PathMappingError as exc:
        return str(exc)
    return ""


class LocalMediaScheduler:
    def __init__(self, *, owner: str = "admin", interval: float = 10.0, service=None, qb_factory=None, clock=None):
        self.owner = owner
        self.interval = max(0.2, float(interval))
        self.service = service or LocalMediaService()
        self.qb_factory = qb_factory or self._default_qb_client
        self._clock = clock or time.monotonic
        self._last_scan_at: dict[int, float] = {}
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._path_locks: set[str] = set()
        self._capture_result_task_ids: set[int] = set()
        self._captured_task_results: dict[int, dict[str, object]] = {}
        self._guard = threading.RLock()

    @staticmethod
    def _default_qb_client():
        return QBittorrentClient(
            url=get("QB_URL"), username=get("QB_USERNAME"), password=get("QB_PASSWORD"),
            api_key=get("QB_API_KEY"),
        )

    def start(self) -> None:
        with self._guard:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="local-media-scheduler",
                daemon=True,
            )
            self._thread.start()
        logger.info("本地媒体调度器已启动")

    def stop(self, timeout: float = 30.0) -> bool:
        with self._guard:
            self._stop_event.set()
            self._wake_event.set()
            thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        stopped = not thread or not thread.is_alive()
        with self._guard:
            if self._thread is thread and stopped:
                self._thread = None
        return stopped

    def reload(self) -> None:
        self._wake_event.set()

    def status(self) -> dict[str, bool | float]:
        """返回无副作用的进程内状态，不启动线程或执行扫描。"""
        with self._guard:
            thread = self._thread
            return {
                "running": bool(thread and thread.is_alive()),
                "interval_seconds": self.interval,
            }

    def enqueue_completed_torrent(self, task: TorrentTask, *, wake: bool = True) -> int | None:
        raw_path = str(task.content_path or "").strip()
        if not raw_path:
            return None
        matches = []
        for source in db.list_local_media_sources(owner=self.owner, enabled_only=True):
            if source.qb_profile and source.qb_profile not in {"qb", "configured:qb", "default"}:
                continue
            source_error = _source_path_error(source)
            try:
                mapping = PathMapping(
                    source.qb_path_prefix or source.local_root,
                    Path("/") if source_error else Path(source.local_root),
                )
                if mapping.matches(raw_path):
                    matches.append((
                        len(mapping.comparison_prefix), source, mapping, source_error,
                    ))
            except PathMappingError:
                continue
        if not matches:
            return None
        longest_prefix = max(item[0] for item in matches)
        strongest_matches = [item for item in matches if item[0] == longest_prefix]
        valid_matches = [item for item in strongest_matches if not item[3]]
        if not valid_matches:
            raise LocalMediaSourceMigrationRequired(strongest_matches[0][3])
        _, source, mapping, _ = valid_matches[0]
        local_path = assert_within(
            mapping.local_root.joinpath(*mapping.relative_parts(raw_path)),
            mapping.local_root,
        )
        try:
            contains_video = LocalFilesystemAdapter(mapping.local_root).contains_video(local_path)
        except (LocalStorageError, OSError) as exc:
            logger.warning("qB 完成内容媒体检查暂时失败 %s: %s", local_path.name, exc)
            raise LocalMediaProbeRetryable(str(exc)) from exc
        if not contains_video:
            return None
        task_id = db.create_local_media_task(
            source.id, task.hash, str(local_path), owner=self.owner, trigger="qb_completed",
        )
        if wake:
            self.reload()
        return task_id

    _source_candidates = staticmethod(discover_local_media_candidates)

    def _enqueue_scan_candidates(self) -> int:
        count = 0
        now = float(self._clock())
        sources = db.list_local_media_sources(owner=self.owner)
        active_ids = {source.id for source in sources}
        self._last_scan_at = {key: value for key, value in self._last_scan_at.items() if key in active_ids}
        for source in sources:
            if not source.scan_enabled or source.mode == "preview_only":
                continue
            last_scan = self._last_scan_at.get(source.id)
            if last_scan is not None and now - last_scan < source.scan_interval_minutes * 60:
                continue
            self._last_scan_at[source.id] = now
            source_error = _source_path_error(source)
            if source_error:
                log_throttled(
                    logger, logging.WARNING, f"invalid_source:{source.id}",
                    "本地媒体来源扫描失败 %s: %s", source.name, source_error,
                    interval_seconds=3600.0,
                )
                continue
            candidates, error = self._source_candidates(source)
            if error:
                logger.warning("本地媒体来源扫描失败 %s: %s", source.name, error)
                continue
            for candidate in candidates:
                try:
                    db.create_local_media_task(
                        source.id, "", str(candidate), owner=self.owner, trigger="scan",
                    )
                    count += 1
                except Exception as exc:
                    logger.warning("本地媒体扫描候选入队失败 %s: %s", candidate.name, exc)
        return count

    def enqueue_manual_scan_candidates(
        self,
        *,
        silent: bool = False,
        capture_results: bool = False,
    ) -> dict[str, object]:
        """显式扫描全部本地来源，把已存在媒体作为手动任务加入队列。

        与定时扫描不同，本入口不要求 ``scan_enabled``，用于 Web/TG 的一次性
        用户操作；仅预览来源和未配置归档目标的来源不会被移动。
        """
        task_ids: list[int] = []
        source_results: list[dict[str, object]] = []
        for source in db.list_local_media_sources(owner=self.owner):
            source_error = _source_path_error(source)
            if source_error:
                source_results.append({
                    "id": source.id, "name": source.name, "candidates": 0,
                    "queued": 0, "skipped": True, "reason": "",
                    "error": source_error,
                })
                continue
            if source.mode == "preview_only":
                source_results.append({
                    "id": source.id, "name": source.name, "candidates": 0,
                    "queued": 0, "skipped": True, "reason": "来源处于仅预览模式",
                    "error": "",
                })
                continue
            if not db.list_local_library_targets(source.id, owner=self.owner):
                source_results.append({
                    "id": source.id, "name": source.name, "candidates": 0,
                    "queued": 0, "skipped": True, "reason": "尚未配置归档目标",
                    "error": "",
                })
                continue
            candidates, error = self._source_candidates(source)
            if error:
                source_results.append({
                    "id": source.id, "name": source.name, "candidates": 0,
                    "queued": 0, "skipped": True, "reason": "", "error": error,
                })
                continue
            source_task_ids: list[int] = []
            for candidate in candidates:
                try:
                    prefix = (
                        SILENT_MANUAL_SCAN_TOKEN_PREFIX
                        if silent else MANUAL_SCAN_TOKEN_PREFIX
                    )
                    task_id = db.create_local_media_task(
                        source.id,
                        "",
                        str(candidate),
                        owner=self.owner,
                        trigger="scan",
                        operation_token=f"{prefix}{uuid.uuid4().hex}",
                    )
                    source_task_ids.append(int(task_id))
                except Exception as exc:
                    logger.warning("本地媒体手动扫描入队失败 %s: %s", candidate.name, exc)
            task_ids.extend(source_task_ids)
            source_results.append({
                "id": source.id,
                "name": source.name,
                "candidates": len(candidates),
                "queued": len(set(source_task_ids)),
                "skipped": False,
                "reason": "",
                "error": "",
            })
        unique_task_ids = list(dict.fromkeys(task_ids))
        if capture_results and unique_task_ids:
            with self._guard:
                self._capture_result_task_ids.update(unique_task_ids)
        if unique_task_ids:
            self.reload()
        return {
            "ok": True,
            "source_count": len(source_results),
            "scanned_sources": sum(1 for item in source_results if not item["skipped"]),
            "candidate_count": sum(int(item["candidates"]) for item in source_results),
            "queued_count": len(unique_task_ids),
            "task_ids": unique_task_ids,
            "sources": source_results,
        }

    def _complete_captured_task_result(
        self,
        task_id: int,
        result: dict[str, object] | None,
    ) -> None:
        normalized_id = int(task_id)
        with self._guard:
            if normalized_id not in self._capture_result_task_ids:
                return
            self._capture_result_task_ids.discard(normalized_id)
            if not isinstance(result, dict):
                return
            self._captured_task_results.pop(normalized_id, None)
            self._captured_task_results[normalized_id] = result
            while len(self._captured_task_results) > _MAX_CAPTURED_TASK_RESULTS:
                oldest_id = next(iter(self._captured_task_results))
                self._captured_task_results.pop(oldest_id, None)

    def take_captured_task_result(self, task_id: int) -> dict[str, object] | None:
        """消费 TG 手动批次的任务结果；其他扫描任务不会进入该缓存。"""
        normalized_id = int(task_id)
        with self._guard:
            self._capture_result_task_ids.discard(normalized_id)
            return self._captured_task_results.pop(normalized_id, None)

    @staticmethod
    def _is_manual_scan_task(task) -> bool:
        token = str(getattr(task, "operation_token", "") or "")
        return token.startswith((MANUAL_SCAN_TOKEN_PREFIX, SILENT_MANUAL_SCAN_TOKEN_PREFIX))

    @staticmethod
    def _is_silent_task(task) -> bool:
        token = str(getattr(task, "operation_token", "") or "")
        return token.startswith(SILENT_MANUAL_SCAN_TOKEN_PREFIX)

    @staticmethod
    def _elapsed_seconds(value: str) -> float:
        if not value:
            return 0.0
        try:
            return max(0.0, (datetime.now() - datetime.strptime(value, "%Y-%m-%d %H:%M:%S")).total_seconds())
        except ValueError:
            return 0.0

    def _process_waiting(self, task) -> bool:
        source = db.get_local_media_source(task.source_id, owner=self.owner)
        manual_scan = self._is_manual_scan_task(task)
        disabled = (
            source is None
            or (task.trigger == "qb_completed" and not source.enabled)
            or (task.trigger == "scan" and not source.scan_enabled and not manual_scan)
        )
        if disabled:
            error = "来源不存在或对应触发方式已停用"
            db.update_local_media_task(
                task.id, owner=self.owner, status="failed", error=error,
            )
            db.update_download_request_for_local_media_task(
                task.id, "failed", error=error,
            )
            return False
        source_error = _source_path_error(source)
        if source_error:
            db.update_local_media_task(
                task.id, owner=self.owner, status="failed", error=source_error,
            )
            db.update_download_request_for_local_media_task(
                task.id, "failed", error=source_error,
            )
            return False
        key = str(Path(task.content_path).expanduser().resolve(strict=False))
        with self._guard:
            if key in self._path_locks:
                return False
            self._path_locks.add(key)
        try:
            if task.stable_since and self._elapsed_seconds(task.stable_since) < source.stable_seconds:
                return False
            snapshots = LocalFilesystemAdapter(Path(source.local_root)).scan(Path(task.content_path))
            digest = snapshot_digest(snapshots)
            if not task.stable_since or task.snapshot_digest != digest:
                db.update_local_media_task(
                    task.id, owner=self.owner, stable_since=db.now(), snapshot_digest=digest,
                )
                task = db.get_local_media_task(task.id, owner=self.owner)
            if self._elapsed_seconds(task.stable_since) < source.stable_seconds:
                return False
            if not db.claim_local_media_task(task.id, expected="waiting_stable", owner=self.owner):
                return False
            qb_client = self.qb_factory() if task.qb_hash else None
            result = self.service.execute_task(self.owner, task.id, qb_client=qb_client)
        except Exception as exc:
            self._complete_captured_task_result(task.id, None)
            current = db.get_local_media_task(task.id, owner=self.owner)
            terminal_statuses = {"completed", "requires_manual", "failed"}
            if current and current.status not in terminal_statuses:
                db.update_local_media_task(
                    task.id, owner=self.owner, status="failed", error=str(exc),
                )
                current = db.get_local_media_task(task.id, owner=self.owner)
            logger.error("本地媒体任务执行失败 task=%s type=%s", task.id, type(exc).__name__)
            if current and current.status in terminal_statuses:
                try:
                    db.update_download_request_for_local_media_task(
                        task.id, current.status, error=str(current.error or exc),
                    )
                except Exception as sync_exc:
                    logger.error(
                        "本地媒体失败状态回写异常 task=%s type=%s",
                        task.id, type(sync_exc).__name__,
                    )
            if not self._is_silent_task(task):
                try:
                    notify_local_media_task(task.id, owner=self.owner, error=str(exc))
                except Exception as notify_exc:
                    logger.warning(
                        "本地媒体失败通知发送异常 task=%s type=%s",
                        task.id, type(notify_exc).__name__,
                    )
            return False
        else:
            captured_result = (
                result
                if str(result.get("status") or "") == "requires_manual"
                else None
            )
            self._complete_captured_task_result(task.id, captured_result)
            try:
                db.update_download_request_for_local_media_task(
                    task.id, str(result.get("status") or "failed"),
                    error=str(result.get("preview", {}).get("reason") or ""),
                )
            except Exception as sync_exc:
                logger.error(
                    "本地媒体完成状态回写异常 task=%s type=%s",
                    task.id, type(sync_exc).__name__,
                )
            if not self._is_silent_task(task):
                try:
                    notify_local_media_task(task.id, result, owner=self.owner)
                except Exception as notify_exc:
                    logger.warning(
                        "本地媒体完成通知发送异常 task=%s type=%s",
                        task.id, type(notify_exc).__name__,
                    )
            return True
        finally:
            with self._guard:
                self._path_locks.discard(key)

    def run_once(self) -> int:
        self._enqueue_scan_candidates()
        processed = 0
        for task in reversed(db.list_local_media_tasks(owner=self.owner, status="waiting_stable", limit=500)):
            processed += int(self._process_waiting(task))
        return processed

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception as exc:
                log_throttled(
                    logger, logging.ERROR, f"local-media-scheduler:{type(exc).__name__}",
                    "本地媒体调度周期失败 type=%s", type(exc).__name__,
                )
            self._wake_event.wait(self.interval)
            self._wake_event.clear()


_scheduler: LocalMediaScheduler | None = None
_scheduler_lock = threading.Lock()


def get_local_media_scheduler() -> LocalMediaScheduler:
    global _scheduler
    with _scheduler_lock:
        if _scheduler is None:
            _scheduler = LocalMediaScheduler()
        return _scheduler


def peek_local_media_scheduler_status() -> dict[str, bool | float]:
    """读取已存在调度器的状态；未初始化时不构造服务或读取配置。"""
    with _scheduler_lock:
        scheduler = _scheduler
    if scheduler is None:
        return {"running": False, "interval_seconds": 0.0}
    return scheduler.status()
