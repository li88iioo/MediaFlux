"""Agent 确定性只读意图的轻量声明与有序匹配。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable


@dataclass(frozen=True, slots=True)
class ReadIntentSpec:
    """一个无需参数、可直接调用的只读工具意图。"""

    tool_name: str
    matcher: Callable[[str], bool]


def match_read_intent(message: str, specs: Iterable[ReadIntentSpec]) -> str | None:
    """按声明顺序返回首个命中的工具名；顺序是路由契约的一部分。"""
    for spec in specs:
        if spec.matcher(message):
            return spec.tool_name
    return None
