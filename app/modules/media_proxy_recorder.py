"""媒体反代播放诊断的有界异步写入器。"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any

from app import database
from app.logger import get_logger

logger = get_logger(__name__)
_STOP = object()
_DEFAULT_CAPACITY = 256
_DEFAULT_DRAIN_TIMEOUT_SECONDS = 3.0


def _write_record(payload: Mapping[str, Any]) -> None:
    database.record_media_proxy_playback_attempt(**dict(payload))


class PlaybackRecordWriter:
    """用单 worker 将播放诊断移出代理事件循环，并保持有界背压。"""

    def __init__(
        self,
        *,
        capacity: int = _DEFAULT_CAPACITY,
        drain_timeout_seconds: float = _DEFAULT_DRAIN_TIMEOUT_SECONDS,
        write_record: Callable[[Mapping[str, Any]], None] | None = None,
        task_name: str = "media-proxy-playback-records",
    ) -> None:
        self._capacity = max(2, int(capacity))
        self._drain_timeout_seconds = max(0.05, float(drain_timeout_seconds))
        self._write_record = write_record or _write_record
        self._task_name = task_name
        self._queue: asyncio.Queue[object] = asyncio.Queue(maxsize=self._capacity)
        self._task: asyncio.Task[None] | None = None
        self._accepting = False
        self._last_drained = True
        self._counters = {
            "enqueued": 0,
            "written": 0,
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
        if self._task is not None and not self._task.done():
            self._accepting = True
            return
        self._accepting = True
        self._last_drained = True
        self._task = asyncio.create_task(self._run(), name=self._task_name)

    def enqueue(self, payload: Mapping[str, Any]) -> bool:
        record = dict(payload)
        if not self._accepting:
            self._count_drop(record, "dropped_stopping")
            return False

        # 为错误状态保留一个槽位；低价值成功诊断先丢弃，但绝不阻塞播放请求。
        if not self._is_critical(record) and self._queue.qsize() >= self._capacity - 1:
            self._count_drop(record, "dropped_full")
            self._counters["dropped_low_priority"] += 1
            return False
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            self._count_drop(record, "dropped_full")
            return False
        self._counters["enqueued"] += 1
        return True

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is _STOP:
                    return
                payload = dict(item) if isinstance(item, Mapping) else {}
                try:
                    await asyncio.to_thread(self._write_record, payload)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._counters["failed"] += 1
                    logger.warning(
                        "媒体反代播放记录异步写入失败 type=%s",
                        type(exc).__name__,
                    )
                else:
                    self._counters["written"] += 1
            finally:
                self._queue.task_done()

    def _drop_queued_for_shutdown(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                if isinstance(item, Mapping):
                    self._count_drop(item, "dropped_shutdown")
            finally:
                self._queue.task_done()

    async def stop(self) -> bool:
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
            "worker_running": bool(task is not None and not task.done()),
            "drained": self._last_drained,
        }
