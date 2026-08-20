"""Media Agent 全局功能开关。"""
from __future__ import annotations

from app import config


AGENT_ENABLED_KEY = "AGENT_ENABLED"


def is_agent_enabled() -> bool:
    """返回 Agent 是否整体启用。

    默认关闭；只有显式配置 ``AGENT_ENABLED=1`` 后才启用。
    既有安装已经保存的开关值不受缺省值调整影响。
    子功能开关只有在总开关开启时才生效。
    """

    return config.get_bool(AGENT_ENABLED_KEY, False)
