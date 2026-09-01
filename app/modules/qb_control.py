"""qBittorrent 控制入口共用的本地整理写入边界。"""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager

from app import database as db
from app.logger import get_logger
from app.modules.process_lock import CrossProcessLock

logger = get_logger(__name__)

# 锁序固定为：local-media-pipeline-write -> qb-control-write。
# 本地整理已经持有前者，只在实际 qB 状态转换时短暂持有后者；独立 qB
# 控制入口只能持有后者，禁止反向进入本地媒体 writer，避免跨进程死锁。
_QB_CONTROL_WRITE_LOCK = CrossProcessLock("qb-control-write")


class QBControlConflict(RuntimeError):
    """qB 操作会干扰正在提交的本地媒体事务。"""


class QBControlSafetyUnavailable(QBControlConflict):
    """无法读取本地整理安全状态，破坏性 qB 操作必须失败关闭。"""


@contextmanager
def qb_control_write_lease() -> Iterator[None]:
    """串行化所有会改变 qB 任务状态的入口。

    调用方必须在 lease 内完成安全检查和真实 qB 请求，不能只保护其中一段。
    """
    try:
        acquired = _QB_CONTROL_WRITE_LOCK.acquire(blocking=True)
    except OSError as exc:
        raise QBControlSafetyUnavailable("qB 控制写入队列暂不可用，请稍后重试") from exc
    if not acquired:  # blocking=True 正常不会返回 False，仅作防御。
        raise QBControlSafetyUnavailable("qB 控制写入队列暂不可用，请稍后重试")
    try:
        yield
    finally:
        _QB_CONTROL_WRITE_LOCK.release()


def assert_qb_control_allowed(hashes: Iterable[str], *, operation: str) -> None:
    """在 qB writer lease 内校验恢复/移除是否会撞上本地整理。

    ``pause`` 是单向收紧状态，即使本地安全表暂时不可读也允许执行；
    ``resume``/``delete`` 会重新激活或移除任务，安全状态不可读时必须拒绝。
    """
    normalized_operation = str(operation or "").strip().casefold()
    if normalized_operation == "pause":
        return
    if normalized_operation not in {"resume", "delete"}:
        raise ValueError("不支持的 qBittorrent 控制操作")
    try:
        conflicts = db.list_local_media_qb_write_conflicts(list(hashes))
    except Exception as exc:
        logger.error(
            "qB 控制安全状态读取失败 operation=%s type=%s",
            normalized_operation,
            type(exc).__name__,
        )
        raise QBControlSafetyUnavailable(
            "本地整理安全状态暂不可用，已拒绝恢复或移除 qB 任务"
        ) from exc
    if conflicts:
        raise QBControlConflict(
            "所选下载任务正在执行本地整理写入，请等待整理完成后再操作"
        )
