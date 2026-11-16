"""媒体反代播放诊断的有界异步写入器。"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.repositories import media_proxy as media_proxy_repo
from app.logger import get_logger

logger = get_logger(__name__)
_STOP = object()
_DEFAULT_CAPACITY = 256
_DEFAULT_DRAIN_TIMEOUT_SECONDS = 3.0


def _write_record(payload: Mapping[str, Any]) -> int:
    return media_proxy_repo.record_media_proxy_playback_attempt(**dict(payload))


def _update_record(record_id: int, *, failure_stage: str, error: str) -> bool:
    return media_proxy_repo.fail_media_proxy_playback_attempt(
        record_id, failure_stage=failure_stage, error=error,
    )


@dataclass(slots=True)
class PlaybackRecordReceipt:
    """仅随请求和队列存活的回执，不维护进程级记录ID映射。"""

    _payload: dict[str, Any] = field(repr=False)
    record_id: int | None = None
    _insert_failed: bool = False
    _failure_queued: bool = False


@dataclass(slots=True)
class _RecordFailure:
    receipt: PlaybackRecordReceipt
    failure_stage: str
    error: str

    def payload(self) -> dict[str, Any]:
        return {
            **self.receipt._payload,
            "status_code": 0,
            "failure_stage": self.failure_stage,
            "error": self.error,
        }


class PlaybackRecordWriter:
    """用单 worker 将播放诊断移出代理事件循环，并保持有界背压。"""

    def __init__(
        self,
        *,
        capacity: int = _DEFAULT_CAPACITY,
        drain_timeout_seconds: float = _DEFAULT_DRAIN_TIMEOUT_SECONDS,
        write_record: Callable[[Mapping[str, Any]], int] | None = None,
        update_record: Callable[..., bool] | None = None,
        task_name: str = "media-proxy-playback-records",
    ) -> None:
        self._capacity = max(2, capacity)
        self._drain_timeout_seconds = max(0.05, float(drain_timeout_seconds))
        self._write_record = write_record or _write_record
        self._update_record = update_record or _update_record
        self._task_name = task_name
        self._queue: asyncio.Queue[object] = asyncio.Queue(maxsize=self._capacity)
        self._task: asyncio.Task[None] | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._accepting = False
        self._last_drained = True
        self._counters = {
            "enqueued": 0,
            "written": 0,
            "updates_enqueued": 0,
            "updated": 0,
            "failed": 0,
            "dropped_full": 0,
            "dropped_low_priority": 0,
            "dropped_stopping": 0,
            "dropped_shutdown": 0,
            "dropped_error": 0,
        }

    @staticmethod
    def _is_critical(payload: Mapping[str, Any]) -> bool:
        try:
            status_code = int(payload.get("status_code", 0) or 0)
        except (TypeError, ValueError):
            status_code = 0
        failure_stage = str(payload.get("failure_stage", "") or "").strip().lower()
        return bool(
            status_code >= 400
            or (failure_stage and failure_stage != "none")
            or str(payload.get("error", "") or "").strip()
        )

    def _count_drop(self, payload: Mapping[str, Any], reason: str) -> None:
        self._counters[reason] += 1
        if self._is_critical(payload):
            self._counters["dropped_error"] += 1

    async def start(self) -> None:
        # start/stop必须串行，不能在旧worker退出前重新开放接收。
        async with self._lifecycle_lock:
            if self._task is not None and not self._task.done():
                self._accepting = True
                return
            self._accepting = True
            self._last_drained = True
            self._task = asyncio.create_task(self._run(), name=self._task_name)

    def enqueue(self, payload: Mapping[str, Any]) -> PlaybackRecordReceipt | None:
        record = dict(payload)
        if not self._accepting:
            self._count_drop(record, "dropped_stopping")
            return None

        # 为错误状态保留一个槽位；低价值成功诊断先丢弃，但绝不阻塞播放请求。
        if not self._is_critical(record) and self._queue.qsize() >= self._capacity - 1:
            self._count_drop(record, "dropped_full")
            self._counters["dropped_low_priority"] += 1
            return None
        receipt = PlaybackRecordReceipt(record)
        try:
            self._queue.put_nowait(receipt)
        except asyncio.QueueFull:
            self._count_drop(record, "dropped_full")
            return None
        self._counters["enqueued"] += 1
        return receipt

    def fail(
        self, receipt: PlaybackRecordReceipt | None, *, failure_stage: str, error: str,
    ) -> bool:
        if receipt is None:
            return False
        if receipt._failure_queued:
            return True
        failure = _RecordFailure(receipt, failure_stage, error)
        if not self._accepting:
            self._count_drop(failure.payload(), "dropped_stopping")
            return False
        try:
            self._queue.put_nowait(failure)
        except asyncio.QueueFull:
            self._count_drop(failure.payload(), "dropped_full")
            return False
        receipt._failure_queued = True
        self._counters["updates_enqueued"] += 1
        return True

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is _STOP:
                    return
                try:
                    if isinstance(item, PlaybackRecordReceipt):
                        item.record_id = await asyncio.to_thread(self._write_record, item._payload)
                        self._counters["written"] += 1
                    elif isinstance(item, _RecordFailure):
                        receipt = item.receipt
                        if receipt._insert_failed:
                            # 原始写入失败时没有记录可更新，只补写一条真实错误。
                            receipt.record_id = await asyncio.to_thread(self._write_record, item.payload())
                            self._counters["written"] += 1
                        else:
                            assert receipt.record_id is not None
                            updated = await asyncio.to_thread(
                                self._update_record, receipt.record_id,
                                failure_stage=item.failure_stage, error=item.error,
                            )
                            if updated:
                                self._counters["updated"] += 1
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if isinstance(item, PlaybackRecordReceipt):
                        item._insert_failed = True
                    self._counters["failed"] += 1
                    logger.warning(
                        "媒体反代播放记录异步写入失败 type=%s",
                        type(exc).__name__,
                    )
            finally:
                self._queue.task_done()

    def _drop_queued_for_shutdown(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                if isinstance(item, PlaybackRecordReceipt):
                    self._count_drop(item._payload, "dropped_shutdown")
                elif isinstance(item, _RecordFailure):
                    self._count_drop(item.payload(), "dropped_shutdown")
            finally:
                self._queue.task_done()

    async def stop(self) -> bool:
        # 多个关闭者只能发布一个STOP标记，防止下次启动立即退出。
        async with self._lifecycle_lock:
            return await self._stop_unlocked()

    async def _stop_unlocked(self) -> bool:
        self._accepting = False
        task = self._task
        if task is None:
            self._last_drained = self._queue.empty()
            return self._last_drained

        drained = True
        try:
            await asyncio.wait_for(
                asyncio.shield(self._queue.join()),
                timeout=self._drain_timeout_seconds,
            )
        except asyncio.TimeoutError:
            drained = False
            self._drop_queued_for_shutdown()
            # to_thread 中已经开始的同步 SQLite 写入无法被 task.cancel() 中止。
            # 丢弃尚未开始的记录后让当前写入真实收敛，再退出 worker，避免
            # stop() 返回后旧 runtime 继续写库或占用默认 executor。
            self._queue.put_nowait(_STOP)
            await asyncio.gather(task, return_exceptions=True)
            logger.warning(
                "媒体反代播放记录关闭排空超时，已丢弃排队记录并等待当前写入完成"
            )
        except asyncio.CancelledError:
            drained = False
            self._drop_queued_for_shutdown()
            self._queue.put_nowait(_STOP)
            await asyncio.shield(asyncio.gather(task, return_exceptions=True))
            raise
        else:
            self._queue.put_nowait(_STOP)
            await asyncio.gather(task, return_exceptions=True)
        finally:
            if task.done():
                self._task = None
            self._last_drained = drained
        return drained

    def metrics(self) -> dict[str, int | bool]:
        task = self._task
        return {
            "capacity": self._capacity,
            "pending": self._queue.qsize(),
            **self._counters,
            "accepting": self._accepting,
            "worker_running": (task is not None and not task.done()),
            "drained": self._last_drained,
        }
