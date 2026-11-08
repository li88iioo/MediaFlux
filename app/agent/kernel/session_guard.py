"""owner/session 文件互斥：确认写持锁至回执落盘，状态事务复用同一边界。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

from app import database as db
from app.modules.process_lock import CrossProcessLock

from .state_types import SessionBusyError

if TYPE_CHECKING:
    from .session_state import SessionState


@dataclass(slots=True)
class _HeldScope:
    key: str
    kind: str
    pid: int
    task: object | None
    active: bool = True


_held_scope: ContextVar[_HeldScope | None] = ContextVar("agent_session_guard", default=None)


def _task() -> object | None:
    try:
        return asyncio.current_task()
    except RuntimeError:  # asyncio.to_thread 会携带 context，但线程内没有事件循环。
        return None


def _scope_key(owner: str, session_id: str) -> str:
    owner, session_id = (owner or "").strip(), (session_id or "").strip()
    if not owner or not session_id:
        raise ValueError("Agent 会话 scope 无效")
    return hashlib.sha256(json.dumps(
        [str(db.resolve_db_path()), owner, session_id], ensure_ascii=False,
        separators=(",", ":"),
    ).encode()).hexdigest()


@contextmanager
def continuation_execution_guard(owner: str, session_id: str) -> Generator[bool, None, None]:
    """续行执行持有独立 OS 锁；进程退出自动释放，不用 TTL 推断死亡。

    与会话状态事务锁分开，存活续行期间仍允许冷取消和前台新请求。
    返回 False 表示该会话已有执行者，调用方不能接管或收束其状态。
    """
    lock = CrossProcessLock("agent-continuation-" + _scope_key(owner, session_id))
    try:
        acquired = lock.acquire(blocking=False)
    except OSError as exc:
        raise SessionBusyError("continuation execution guard is unavailable") from exc
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()


def _foreground_lock(owner: str, session_id: str, lifecycle_id: str, generation: int, turn_id: str = "") -> CrossProcessLock:
    identity = hashlib.sha256((f"{lifecycle_id}:{generation}" + (f":{turn_id}" if turn_id else "")).encode()).hexdigest()
    return CrossProcessLock(f"agent-foreground-{_scope_key(owner, session_id)}-{identity}")


@contextmanager
def foreground_execution_guard(owner: str, session_id: str, lifecycle_id: str,
                               generation: int, *, turn_id: str = "") -> Generator[bool, None, None]:
    """前台执行活性；在普通/确认准入的会话互斥窗口内取得，至执行收尾释放。

    身份每轮唯一，旧回合不能阻挡新请求。固定会话锁不能删除；这里的
    一次性文件不会再次授予执行权，退出后可清理，崩溃残留在下次准入清理。
    """
    lock = _foreground_lock(owner, session_id, lifecycle_id, generation, turn_id)
    acquired = lock.acquire(blocking=False)
    try:
        if acquired and not turn_id:
            prefix = f".mediaflux-agent-foreground-{_scope_key(owner, session_id)}-"
            for path in lock.path.parent.glob(prefix + "*.lock"):
                if path != lock.path:
                    with suppress(OSError):
                        path.unlink(missing_ok=True)  # begin_turn 已撤销这些旧代的发布权。
        yield acquired
    finally:
        if acquired:
            lock.release()
            with suppress(OSError):
                lock.path.unlink(missing_ok=True)


def foreground_execution_active(owner: str, session_id: str, lifecycle_id: str, generation: int, *, turn_id: str = "") -> bool:
    """仅在会话状态事务内探测当前代，文件存在并不代表执行者存活。"""
    lock = _foreground_lock(owner, session_id, lifecycle_id, generation, turn_id)
    try:
        lock.path.stat()
    except FileNotFoundError:
        return False
    if not lock.acquire(blocking=False):
        return True
    lock.release()
    with suppress(OSError):
        lock.path.unlink(missing_ok=True)
    return False


def publication_execution_active(state: SessionState, *, include_continuation: bool = False) -> bool:
    """按当前发布 fence 探测执行者，忽略已被新确认接管的旧普通回合。"""
    active = state.metadata.get("active_publication") or {}
    if active.get("generation") != state.generation:
        return False
    if foreground_execution_active(state.owner, state.session_id, state.lifecycle_id, state.generation,
                                   turn_id=active.get("execution_turn_id", "")):
        return True
    if not include_continuation:
        return False
    records = [state.metadata.get("read_wait") or {}, *(
        (row.get("continuation") or {}) for row in (state.metadata.get("effect_waits") or {}).values()
    )]
    if any(record.get("publication_generation") == state.generation
           and record.get("resume_turn_id") == active.get("turn_id")
           and record.get("state") in {"claimed", "running"} for record in records):
        with continuation_execution_guard(state.owner, state.session_id) as acquired:
            return not acquired
    return False


@contextmanager
def session_scope_guard(owner: str, session_id: str, *, kind: str = "mutation") -> Generator[None, None, None]:
    """非阻塞互斥；不创建内存锁注册表，也不依赖 TTL 判定活跃执行者。

    同一操作通过 to_thread 进入状态事务时允许复用持锁权。其它 asyncio
    task、进程或已退出的 context 不能继承该权利；确认体内也不能重入 reset。
    锁文件不 unlink，避免新 inode 与仍持有旧 inode 的 worker 分裂互斥域。
    """
    key = _scope_key(owner, session_id)
    inherited = _held_scope.get()
    task = _task()
    if (
        inherited is not None and inherited.active and inherited.key == key
        and inherited.pid == os.getpid()
        and (task is None or task is inherited.task)
    ):
        if kind != inherited.kind and kind != "commit":
            raise SessionBusyError("confirmed effect is executing")
        yield
        return

    lock = CrossProcessLock("agent-session-" + key)
    try:
        acquired = lock.acquire(blocking=False)
    except OSError as exc:
        raise SessionBusyError("session execution guard is unavailable") from exc
    if not acquired:
        raise SessionBusyError("session operation is executing")
    held = _HeldScope(key, kind, os.getpid(), task)
    token = _held_scope.set(held)
    try:
        yield
    finally:
        held.active = False
        _held_scope.reset(token)
        lock.release()


async def guarded_state_call(
    owner: str, session_id: str, call: Callable[..., Any], *args: Any,
    kind: str = "mutation", complete_on_cancel: bool = False,
) -> Any:
    """先在当前 task 验证持锁权，再进入线程；取消不能先于 DB 事务放锁。"""
    with session_scope_guard(owner, session_id, kind=kind):
        return await run_state_worker(call, args, complete_on_cancel=complete_on_cancel)


def _effect_active() -> bool:
    held = _held_scope.get()
    return bool(held and held.active and held.kind == "effect"
                and held.pid == os.getpid() and held.task is _task())


async def session_io(call: Callable[..., Any], *args: Any) -> Any:
    """确认持有者的持久化收尾不可被请求取消打断；普通读请求仍可取消。"""
    if _effect_active():
        return await run_state_worker(call, args, complete_on_cancel=True)
    return await asyncio.to_thread(call, *args)


class _CancelledWorkerResult(asyncio.CancelledError):
    """调用方取消后工作项仍正常返回，保留实际结果供持锁精确清理。"""

    def __init__(self, result: Any) -> None:
        super().__init__()
        self.result = result


async def run_state_worker(call: Callable[..., Any], args: tuple[Any, ...], *, complete_on_cancel: bool = False,
                           scope: tuple[str, str] | None = None) -> Any:
    context = copy_context()
    held = _held_scope.get()
    key = _scope_key(*scope) if scope is not None else None
    complete_on_cancel = complete_on_cancel or (_effect_active() and (key is None or (held is not None and held.key == key)))
    if held is not None and (not held.active or held.pid != os.getpid() or held.task is not _task()):
        if held.active and held.pid == os.getpid() and held.key == key:
            raise SessionBusyError("session operation is executing")
        # 子Task会复制ContextVar，但不能把父Task的重入权带进_task=None的线程。
        context.run(_held_scope.set, None)
    # 使用 executor Future 而非额外 Task：事件循环 shutdown 的全 Task
    # 取消不能把仍在执行的同步线程误判为结束，进而提前释放会话文件锁。
    operation = asyncio.get_running_loop().run_in_executor(
        None, partial(context.run, call, *args),
    )
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(operation)
            break
        except asyncio.CancelledError:
            cancelled = True
            if operation.done():
                result = operation.result()  # 工作项自身取消/失败必须传播，不能反复 await。
                break
    if cancelled and not complete_on_cancel:
        raise _CancelledWorkerResult(result)
    return result
