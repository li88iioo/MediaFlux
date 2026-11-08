"""原子工具事实目录；模型入口由宿主显式指定。"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class ToolEffect(StrEnum):
    READ = "read"
    WRITE = "write"
    DANGER = "danger"


ToolCallable = Callable[..., Any]
Validator = Callable[[dict[str, Any]], dict[str, Any]]
Availability = Callable[[Mapping[str, Any]], bool]
Authorizer = Callable[[Mapping[str, Any]], bool]


def identity_validator(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise TypeError("tool arguments must be an object")
    return dict(arguments)


@dataclass(frozen=True, slots=True, kw_only=True)
class KernelToolSpec:
    name: str
    domain: str
    description: str
    input_schema: Mapping[str, Any]
    effect: ToolEffect
    effect_resolver: Callable[[Mapping[str, Any]], ToolEffect] | None = None
    examples: tuple[str, ...] = ()
    cost: float = 1.0
    context_requirements: frozenset[str] = frozenset()
    validator: Validator = identity_validator
    read: ToolCallable | None = None
    observe: ToolCallable | None = None
    prepare: ToolCallable | None = None
    execute_confirmed: ToolCallable | None = None
    verify: ToolCallable | None = None
    availability: Availability | None = None
    runtime_status: Callable[[], Mapping[str, str]] | None = None
    authorize: Authorizer | None = None
    approval_scope: Callable[[str, Mapping[str, Any], Mapping[str, Any]], dict[str, str] | None] | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    model_name: str = ""

    def __post_init__(self) -> None:
        name = (self.name or "").strip()
        domain = (self.domain or "").strip().casefold()
        description = (self.description or "").strip()
        if not name or not re.fullmatch(r"[a-z][a-z0-9_.-]{2,119}", name):
            raise ValueError(f"invalid tool name: {name!r}")
        if not domain or not re.fullmatch(r"[a-z][a-z0-9_.-]{1,79}", domain):
            raise ValueError(f"invalid tool domain: {domain!r}")
        if not description:
            raise ValueError("tool description cannot be empty")
        if not isinstance(self.input_schema, Mapping):
            raise TypeError("input_schema must be an object")
        if self.effect is ToolEffect.READ and self.effect_resolver is not None:
            raise ValueError("READ tools cannot resolve write effects")
        if self.effect is ToolEffect.READ:
            if (
                self.read is None
                or self.prepare is not None
                or self.execute_confirmed is not None
            ):
                raise ValueError("READ tool must expose only a read handler")
        elif (
            self.prepare is None
            or self.execute_confirmed is None
            or self.read is not None
        ):
            raise ValueError(
                "WRITE/DANGER tool must expose prepare and execute_confirmed handlers"
            )
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "domain", domain)
        object.__setattr__(self, "description", description)
        object.__setattr__(
            self,
            "examples",
            tuple(item.strip() for item in self.examples if item.strip()),
        )
        object.__setattr__(
            self,
            "context_requirements",
            frozenset(
                item.strip().casefold()
                for item in self.context_requirements
                if item.strip()
            ),
        )
        object.__setattr__(self, "cost", max(0.0, float(self.cost)))
        model_name = (self.model_name or name.replace(".", "__").replace("-", "_")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", model_name):
            raise ValueError(f"invalid provider tool name: {model_name!r}")
        object.__setattr__(self, "model_name", model_name)

    def for_arguments(self, arguments: Mapping[str, Any]) -> KernelToolSpec:
        """参数可提升组合风险，不能把目录声明的风险降级。"""
        if self.effect_resolver is None:
            return self
        effect = self.effect_resolver(arguments)
        if not isinstance(effect, ToolEffect) or effect not in {self.effect, ToolEffect.DANGER}:
            raise ValueError("resolved tool effect cannot downgrade risk")
        return replace(self, effect=effect, effect_resolver=None)

    def model_definition(self) -> dict[str, Any]:
        description = self.description
        if self.effect is not ToolEffect.READ and self.metadata.get("confirmation_required") is not False:
            description += " 调用本工具只准备确认卡，不执行写入，无需先征求生成卡片的许可；实际写入必须等用户确认。"
        return {
            "name": self.model_name,
            "description": description,
            "parameters": dict(self.input_schema),
        }


class ToolCatalog:
    """只保存工具事实，不理解用户自然语言。"""

    def __init__(self, tools: Iterable[KernelToolSpec] = (), *, projector: Any = None, selection: Any = None) -> None:
        self.projector = projector
        self.selection = selection
        self._tools: dict[str, KernelToolSpec] = {}
        self._aliases: dict[str, str] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: KernelToolSpec) -> None:
        if tool.name in self._tools or tool.model_name in self._aliases:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool
        self._aliases[tool.model_name] = tool.name

    def get(self, name: str) -> KernelToolSpec:
        try:
            key = (name or "").strip()
            return self._tools[self._aliases.get(key, key)]
        except KeyError as exc:
            raise KeyError(f"unknown tool: {name}") from exc

    def visible(
        self, context: Mapping[str, Any] | None = None
    ) -> tuple[KernelToolSpec, ...]:
        runtime = context or {}
        result: list[KernelToolSpec] = []
        available_kinds = {
            str(item).strip().casefold()
            for item in runtime.get("reference_kinds", ())
            if str(item).strip()
        }
        for tool in self._tools.values():
            if tool.context_requirements and not tool.context_requirements.issubset(
                available_kinds
            ):
                continue
            if tool.availability is not None:
                try:
                    if not tool.availability(runtime):
                        continue
                except Exception as exc:  # noqa: BLE001 - optional plug-in boundary
                    logger.warning(
                        "Agent 工具可用性检查失败 tool=%s type=%s",
                        tool.name,
                        type(exc).__name__,
                    )
                    continue
            result.append(tool)
        return tuple(result)

    def has(self, name: str) -> bool:
        key = (name or "").strip()
        return key in self._tools or key in self._aliases

    def __len__(self) -> int:
        return len(self._tools)
