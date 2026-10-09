"""回合内能力发现；只更新下一次模型调用的工具窗口，不执行领域动作。"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from .capabilities import CapabilityRetriever, KernelToolSpec, ToolCatalog
from .pipeline import ToolPipelineError

logger = logging.getLogger(__name__)

DISCOVERY_TOOL = "agent.capabilities"


class CapabilityDiscovery:
    """每回合独立。已批准范围、权限、工具执行仍由原 ToolPipeline 管理。"""

    def __init__(
        self, catalog: ToolCatalog, *, context: Mapping[str, Any], maximum: int = 10
    ):
        self.catalog = catalog
        self.context = dict(context)
        self.maximum = max(2, min(12, maximum))
        self.requests = 0
        self._pending: tuple[KernelToolSpec, ...] = ()

    def _visible(self) -> tuple[KernelToolSpec, ...]:
        result = []
        for tool in self.catalog.visible(self.context):
            if tool.name == "agent.read_result" and "tool_result" not in self.context.get("reference_kinds", ()):
                continue
            try:
                if tool.authorize is not None and not tool.authorize(self.context):
                    continue
            except Exception as exc:  # noqa: BLE001 - optional capability policy fails closed
                logger.warning(
                    "Agent能力授权检查失败 tool=%s type=%s",
                    tool.name,
                    type(exc).__name__,
                )
                continue
            result.append(tool)
        return tuple(result)

    def window(
        self,
        previous: Sequence[KernelToolSpec],
        additions: Sequence[KernelToolSpec] = (),
    ) -> tuple[KernelToolSpec, ...]:
        visible = {tool.name: tool for tool in self._visible()}
        ordered = []
        if DISCOVERY_TOOL in visible:
            ordered.append(visible[DISCOVERY_TOOL])
        if "agent.read_result" in visible:
            ordered.append(visible["agent.read_result"])
        for tool in (*additions, *previous):
            if tool.name in visible and all(item.name != tool.name for item in ordered):
                ordered.append(visible[tool.name])
        return tuple(ordered[: self.maximum + int("agent.read_result" in visible)])

    @staticmethod
    def _status(tool: KernelToolSpec) -> dict[str, str]:
        unchecked = {
            "status": "unchecked",
            "reason": "能力已注册；网络、额度与业务运行条件尚未检查。",
        }
        if tool.runtime_status is None:
            return unchecked
        try:
            status = dict(tool.runtime_status())
        except Exception as exc:  # noqa: BLE001 - optional status probe is not a health guarantee
            logger.warning(
                "Agent能力状态检查失败 tool=%s type=%s", tool.name, type(exc).__name__
            )
            return {
                "status": "unknown",
                "reason": "无法读取当前运行条件；不能据此宣称未接入该能力。",
            }
        if status.get("status") not in {
            "configured",
            "disabled",
            "configuration_missing",
            "unchecked",
        }:
            return unchecked
        return {
            "status": str(status["status"]),
            "reason": str(status.get("reason", ""))[:240],
        }

    def search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        visible = self._visible()
        by_name = {tool.name: tool for tool in visible}
        domains = [
            {"domain": domain, "count": count}
            for domain, count in sorted(Counter(t.domain for t in visible).items())
        ]
        if not arguments:
            return {
                "domains": domains,
                "visible_count": len(visible),
                "hint": "用 query 描述缺少的能力；工具未出现在当前窗口不等于项目没有。",
            }
        if self._pending:
            raise ToolPipelineError(
                "本批已有成功的能力发现，请在下一次模型调用使用已加载工具或继续发现。",
                code="capability_discovery_pending",
            )
        if self.requests >= 4:
            raise ToolPipelineError(
                "本轮能力发现已达4次上限，请依据现有结果回答并明确尚未完成部分。",
                code="capability_discovery_budget",
            )
        self.requests += 1
        requested = arguments.get("tool_names", [])
        missing = []
        found: list[KernelToolSpec] = []
        if requested:
            for name in requested:
                try:
                    tool = self.catalog.get(name)
                except KeyError:
                    tool = None
                if tool is None or tool.name not in by_name:
                    missing.append(name)
                elif tool not in found:
                    found.append(tool)
        else:
            selection = CapabilityRetriever(minimum=1, maximum=6).retrieve(
                arguments["query"],
                ToolCatalog(visible),
                context=self.context,
            )
            # 重新检索不继承话题历史；只有正相关或其明确上游才进入窗口。
            found = [
                tool
                for tool in selection.tools
                if selection.scores.get(tool.name, 0) > 0
            ]
        self._pending = tuple(tool for tool in found if tool.name != DISCOVERY_TOOL)[
            : min(6, self.maximum - 1)
        ]
        return {
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description[:500],
                    "effect": tool.effect.value,
                    "availability": self._status(tool),
                    "related_tools": [
                        name
                        for name in tool.metadata.get("related_tools", ())
                        if name in by_name
                    ][:6],
                }
                for tool in found[:6]
            ],
            "not_available_in_context": missing,
            "domains": domains if not found else [],
            "loaded_tools": [tool.name for tool in self._pending],
            "remaining_discoveries": 4 - self.requests,
            "execution": "未执行任何业务动作；发现的工具仅在下一次模型调用加载。写操作仍需确认。",
        }

    def checkpoint(self) -> tuple[KernelToolSpec, ...]:
        return self._pending

    def restore(self, checkpoint: tuple[KernelToolSpec, ...]) -> None:
        self._pending = checkpoint

    def consume(self) -> tuple[KernelToolSpec, ...]:
        result, self._pending = self._pending, ()
        return result
