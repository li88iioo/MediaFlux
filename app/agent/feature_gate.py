"""Media Agent 全局功能开关。"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import threading

from app import config


AGENT_ENABLED_KEY = "AGENT_ENABLED"
_runtime_generation_lock = threading.Lock()
_runtime_admission_lock = threading.RLock()
_runtime_generation = 0


def is_agent_enabled() -> bool:
    """返回 Agent 是否整体启用。

    默认关闭；只有显式配置 ``AGENT_ENABLED=1`` 后才启用。
    既有安装已经保存的开关值不受缺省值调整影响。
    子功能开关只有在总开关开启时才生效。
    """

    return config.get_bool(AGENT_ENABLED_KEY, False)


def current_agent_runtime_generation() -> int:
    """返回当前进程内 Agent 运行代次，用于隔离开关切换前的旧任务。"""
    with _runtime_generation_lock:
        return _runtime_generation


def invalidate_agent_runtime_generation() -> int:
    """递增运行代次，使已开始但尚未发布结果的旧任务立即失效。"""
    global _runtime_generation
    with _runtime_generation_lock:
        _runtime_generation += 1
        return _runtime_generation


def agent_runtime_generation_is_current(generation: int) -> bool:
    """仅当运行代次未变化时允许任务继续发布副作用。"""
    with _runtime_generation_lock:
        return int(generation) == _runtime_generation


class AgentRuntimeDisabled(RuntimeError):
    """Agent 运行态已关闭，新的受控写操作不得进入。"""


@contextmanager
def agent_runtime_transition() -> Iterator[None]:
    """串行化运行态开关切换与不可逆确认写操作的准入。"""
    with _runtime_admission_lock:
        yield


@contextmanager
def agent_runtime_admission(
    *,
    require_telegram: bool = False,
    agent_enabled_check: Callable[[], bool] | None = None,
    telegram_enabled_check: Callable[[], bool] | None = None,
) -> Iterator[int]:
    """在线性化边界内准入并执行一次确认写操作。

    持锁期间关闭动作会等待；若关闭先完成，则新的确认会被拒绝。这样不会
    出现“关闭已返回成功，旧确认随后才开始产生副作用”的 TOCTOU。
    """
    with _runtime_admission_lock:
        if not (agent_enabled_check or is_agent_enabled)():
            raise AgentRuntimeDisabled("Media Agent 已关闭")
        telegram_enabled = telegram_enabled_check or (
            lambda: config.get_bool("TG_AGENT_ENABLED", False)
        )
        if require_telegram and not telegram_enabled():
            raise AgentRuntimeDisabled("Telegram Agent 已关闭")
        generation = current_agent_runtime_generation()
        yield generation


@contextmanager
def agent_runtime_effect_admission(generation: int) -> Iterator[None]:
    """线性化后台外部副作用与 Agent 总开关切换。

    后台任务在领取时已经记录运行代次；这里不重复解释子功能配置，只保证
    总开关关闭并返回后，旧代次不会再开始 Telegram 等不可撤销外部动作。
    """
    with _runtime_admission_lock:
        if not agent_runtime_generation_is_current(generation):
            raise AgentRuntimeDisabled("Media Agent 运行代次已失效")
        yield
