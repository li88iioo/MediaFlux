"""qB 完成、定时扫描与稳定等待驱动的本地媒体调度器。"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from app import database as db
from app.clients.qbittorrent import QBittorrentClient, TorrentTask
from app.config import get
from app.logger import get_logger
from app.modules.local_media_service import LocalMediaService
from app.modules.local_media_candidates import discover_local_media_candidates
from app.modules.local_media_notifications import notify_local_media_task
from app.modules.local_path_mapping import PathMapping, PathMappingError, assert_within
from app.modules.local_storage import LocalFilesystemAdapter, snapshot_digest

logger = get_logger(__name__)


MANUAL_SCAN_TOKEN_PREFIX = "manual-scan:"
SILENT_MANUAL_SCAN_TOKEN_PREFIX = "silent-manual-scan:"


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

    def stop(self, timeout: float = 30.0) -> None:
        with self._guard:
            self._stop_event.set()
            self._wake_event.set()
            thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        with self._guard:
            if self._thread is thread and (not thread or not thread.is_alive()):
                self._thread = None

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
            try:
                mapping = PathMapping(source.qb_path_prefix or source.local_root, Path(source.local_root))
                if mapping.matches(raw_path):
                    matches.append((len(mapping.comparison_prefix), source, mapping))
            except PathMappingError:
                continue
        if not matches:
            return None
        _, source, mapping = max(matches, key=lambda item: item[0])
        local_path = assert_within(
            mapping.local_root.joinpath(*mapping.relative_parts(raw_path)),
            mapping.local_root,
        )
        try:
            if not LocalFilesystemAdapter(mapping.local_root).contains_video(local_path):
                return None
        except Exception as exc:
            logger.warning("qB 完成内容媒体检查失败 %s: %s", local_path.name, exc)
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

    def enqueue_manual_scan_candidates(self, *, silent: bool = False) -> dict[str, object]:
        """显式扫描全部本地来源，把已存在媒体作为手动任务加入队列。

        与定时扫描不同，本入口不要求 ``scan_enabled``，用于 Web/TG 的一次性
        用户操作；仅预览来源和未配置归档目标的来源不会被移动。
        """
        task_ids: list[int] = []
        source_results: list[dict[str, object]] = []
        for source in db.list_local_media_sources(owner=self.owner):
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
        from app.modules.windows_smb import ensure_smb_connection, parse_unc_share_root
        if parse_unc_share_root(source.local_root):
            ok, err = ensure_smb_connection(source.local_root, getattr(source, "smb_user", ""), getattr(source, "smb_pass", ""))
            if not ok and err:
                db.update_local_media_task(
                    task.id, owner=self.owner, status="failed", error=err,
                )
                db.update_download_request_for_local_media_task(
                    task.id, "failed", error=err,
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
            if not self._is_silent_task(task):
                notify_local_media_task(task.id, result, owner=self.owner)
            db.update_download_request_for_local_media_task(
                task.id, str(result.get("status") or "failed"),
                error=str(result.get("preview", {}).get("reason") or ""),
            )
            return True
        except Exception as exc:
            current = db.get_local_media_task(task.id, owner=self.owner)
            if current and current.status != "failed":
                db.update_local_media_task(task.id, owner=self.owner, status="failed", error=str(exc))
            logger.error(f"本地媒体任务执行失败 task={task.id}: {exc}")
            if not self._is_silent_task(task):
                notify_local_media_task(task.id, owner=self.owner, error=str(exc))
            db.update_download_request_for_local_media_task(task.id, "failed", error=str(exc))
            return False
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
                logger.error(f"本地媒体调度周期失败: {exc}")
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
