"""Media Agent 全局功能开关。"""
from __future__ import annotations

import threading

from app import config


AGENT_ENABLED_KEY = "AGENT_ENABLED"
_runtime_generation_lock = threading.Lock()
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
