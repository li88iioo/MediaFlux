"""qBittorrent 控制入口共用的本地整理写入边界。"""
from __future__ import annotations

from collections.abc import Generator, Iterable
from contextlib import ExitStack, contextmanager

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
def qb_control_write_lease() -> Generator[None, None, None]:
    """串行化控制入口与本地整理的 qB 状态转换。

    调用方必须在 lease 内完成安全检查和真实 qB 请求，不能只保护其中一段。
    不同任务的提交可并发，提交与移除另由同一任务身份锁协调。
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


@contextmanager
def qb_submission_write_lease(hashes: Iterable[str]) -> Generator[None, None, None]:
    """同一任务的受理与移除互斥，不串行化不同资源的批量提交。

    控制入口先持有全局控制锁，再按稳定顺序取得任务锁；提交只持有任务锁。
    未知身份的历史 HTTP 提交仍由调用方使用全局控制锁保护。
    """
    with ExitStack() as held:
        for task_id in sorted({value.strip().lower() for value in hashes}):
            lock = CrossProcessLock(f"qb-submit-{task_id}")
            try:
                acquired = lock.acquire(blocking=True)
            except OSError as exc:
                raise QBControlSafetyUnavailable("qB 提交状态暂不可用，请稍后重试") from exc
            if not acquired:
                raise QBControlSafetyUnavailable("qB 提交状态暂不可用，请稍后重试")
            held.callback(lock.release)
        yield


def assert_qb_control_allowed(hashes: Iterable[str], *, operation: str) -> None:
    """在 qB writer lease 内校验恢复/移除是否会撞上本地整理。

    ``pause`` 是单向收紧状态，即使本地安全表暂时不可读也允许执行；
    ``resume``/``delete`` 会重新激活或移除任务，安全状态不可读时必须拒绝。
    """
    normalized_operation = (operation or "").strip().casefold()
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


class QBTaskRemovalUnconfirmed(RuntimeError):
    """跟踪已按用户意图停止，但远端移除未确认。"""


def remove_qb_tasks(client, hashes: Iterable[str]) -> bool:
    """Web / Agent 的用户移除共用入口；调用方必须持有 qB writer lease。

    本地入库完成后的自动删种不能走此入口：那是成功收尾，不是用户取消。
    先落盘停止跟踪意图，防止远端成功、回包丢失或重启后旧请求再次告警。
    """
    normalized = list(dict.fromkeys((value or "").strip().lower() for value in hashes))
    with qb_submission_write_lease(normalized):
        try:
            changed = db.cancel_qb_download_tracking(normalized)
        except Exception as exc:
            raise QBControlSafetyUnavailable("下载跟踪状态暂不可用，未执行 qB 任务移除") from exc
        if changed:
            logger.info("用户移除 qB 任务，已停止 %s 条未完成下载请求的跟踪", len(changed))
        try:
            if not client.delete_torrents("|".join(normalized), delete_files=False):
                raise RuntimeError("qB 未确认接收任务移除")
        except Exception as exc:
            raise QBTaskRemovalUnconfirmed(
                "相关未完成下载的本地跟踪已停止，但 qB 任务移除未确认；"
                "请核对 qB 实时任务。不会删除已下载文件，也不会重新提交下载。"
            ) from exc
    return True
