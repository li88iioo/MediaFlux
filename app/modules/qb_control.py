"""qBittorrent 控制入口共用的本地整理写入边界。"""
from __future__ import annotations

import re
from collections.abc import Callable, Generator, Iterable, Mapping
from contextlib import ExitStack, contextmanager
from typing import Any

from app import config
from app.repositories import download_requests as download_requests_repo
from app.repositories import local_media as local_media_repo
from app.clients.qbittorrent import QBittorrentClient, close_qbittorrent_client
from app.logger import get_logger
from app.modules.process_lock import CrossProcessLock
from app.modules.web_secret import signed_context_fingerprint

logger = get_logger(__name__)
QB_PAUSED_STATES = frozenset({"paused", "pauseddl", "pausedup", "stopped", "stoppeddl", "stoppedup"})
_QB_SETTINGS = ("QB_URL", "QB_USERNAME", "QB_PASSWORD", "QB_API_KEY")


class QBControlStale(RuntimeError):
    """确认过的下载器或任务身份已变化，尚未执行控制操作。"""


def qb_profile_revision(values: Mapping[str, Any]) -> str:
    return signed_context_fingerprint({
        "profile_ref": "configured:qbittorrent",
        "url": str(values.get("QB_URL") or "").strip().rstrip("/"),
        "username": str(values.get("QB_USERNAME") or "").strip(),
        "password": str(values.get("QB_PASSWORD") or ""),
        "api_key": str(values.get("QB_API_KEY") or "").strip(),
    }, domain="provider-profile-revision")


def configured_qb_client(values: Mapping[str, Any]) -> QBittorrentClient:
    url, username, password, api_key = (str(values.get(key) or "") for key in _QB_SETTINGS)
    if not url.strip() or (not api_key.strip() and not (username.strip() and password)):
        raise ValueError("qBittorrent 尚未配置")
    return QBittorrentClient(url=url.strip(), username=username.strip(), password=password,
                            api_key=api_key.strip(), timeout=10)


def _control_hashes(operation: str, hashes: list[str]) -> list[str]:
    if operation not in {"pause", "resume", "delete"}:
        raise ValueError("不支持的 qBittorrent 控制操作")
    if (not isinstance(hashes, list) or not 1 <= len(hashes) <= 20
            or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value.strip())
                   for value in hashes)):
        raise ValueError("hashes 必须包含 1 到 20 个真实 qB 任务 hash")
    return sorted({value.strip().lower() for value in hashes})


def _selected_tasks(client: QBittorrentClient, hashes: list[str]) -> list[Any]:
    wanted = set(hashes)
    return sorted((task for task in client.list_torrents() if task.hash.lower() in wanted),
                  key=lambda task: task.hash.lower())


def _task_identity(task) -> dict[str, Any]:
    # 下载进度/运行状态可以自然变化；同 hash 重新添加或改名不沿用旧确认。
    return {"hash": task.hash.lower(), "name": task.name, "size": task.size, "added_on": task.added_on}


def prepare_qb_control(operation: str, hashes: list[str]) -> dict[str, Any]:
    """只读冻结真实任务和下载器配置；不新增任务或持久状态。"""
    hashes = _control_hashes(operation, hashes)
    settings = config.get_many(_QB_SETTINGS)
    client = configured_qb_client(settings)
    try:
        tasks = _selected_tasks(client, hashes)
        if len(tasks) != len(hashes):
            raise QBControlStale("部分 qBittorrent 任务已不存在，请重新查询")
        assert_qb_control_allowed(hashes, operation=operation)
        return {"operation": operation, "profile_revision": qb_profile_revision(settings),
                "targets": [_task_identity(task) for task in tasks]}
    finally:
        close_qbittorrent_client(client)


def execute_qb_control(snapshot: dict[str, Any], *,
                       cancel_check: Callable[[], None] | None = None) -> dict[str, Any]:
    """原确认快照只提交一次；同一控制锁内复验、安全检查、写入和真实状态读回。"""
    operation = snapshot["operation"]
    hashes = _control_hashes(operation, [item["hash"] for item in snapshot["targets"]])
    if cancel_check:
        cancel_check()
    settings = config.get_many(_QB_SETTINGS)
    if qb_profile_revision(settings) != snapshot["profile_revision"]:
        raise QBControlStale("qBittorrent 配置已变化，请重新预检")
    action = {"pause": "暂停", "resume": "恢复", "delete": "移除并保留文件"}[operation]
    receipt = {"operation": operation, "hashes": hashes, "affected": len(hashes),
               "delete_files": False, "verification": "unknown", "observed_count": 0}
    with qb_control_write_lease():
        if cancel_check:
            cancel_check()
        client = configured_qb_client(settings)
        try:
            tasks = _selected_tasks(client, hashes)
            if [_task_identity(task) for task in tasks] != snapshot["targets"]:
                raise QBControlStale("qBittorrent 任务已变化，请重新查询后确认")
            assert_qb_control_allowed(hashes, operation=operation)
            if cancel_check:
                cancel_check()
            try:
                if operation == "delete":
                    accepted = remove_qb_tasks(client, hashes)
                else:
                    method = client.pause_torrents if operation == "pause" else client.resume_torrents
                    accepted = method("|".join(hashes))
            except QBControlSafetyUnavailable:
                raise  # remove 的本地取消意图未能落盘，尚未向 qB 发出写请求。
            except Exception:
                accepted = None
            if not accepted:
                return {**receipt, "ok": False, "status": "outcome_unknown", "accepted": None,
                        "summary": f"qBittorrent {action}结果未知，请核对实时任务，勿重复提交",
                        "tracking_stopped": operation == "delete"}
            try:
                after = _selected_tasks(client, hashes)
            except Exception:
                return {**receipt, "ok": False, "status": "outcome_unknown", "accepted": True,
                        "summary": f"qBittorrent 已接受{action}请求，但状态读取失败；请核对实时任务，勿重复提交"}
            if operation == "delete":
                verification = "verified" if not after else "pending"
            elif len(after) != len(hashes):
                verification = "partial"
            else:
                states = {task.state.strip().lower() for task in after}
                verified = (states <= QB_PAUSED_STATES if operation == "pause"
                            else not states.intersection(QB_PAUSED_STATES | {"", "unknown", "error", "missingfiles"}))
                verification = "verified" if verified else "pending"
            return {**receipt, "ok": True, "accepted": True, "verification": verification,
                    "status": "completed" if verification == "verified" else "accepted",
                    "observed_count": len(after),
                    "summary": f"qBittorrent 已核验 {len(hashes)} 个任务的{action}结果" if verification == "verified"
                               else f"qBittorrent 已接受 {len(hashes)} 个任务的{action}请求，状态尚未全部核验"}
        finally:
            close_qbittorrent_client(client)

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
        conflicts = local_media_repo.list_local_media_qb_write_conflicts(list(hashes))
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
            changed = download_requests_repo.cancel_qb_download_tracking(normalized)
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
