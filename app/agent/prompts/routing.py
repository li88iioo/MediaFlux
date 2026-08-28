"""结构化意图选择与只读计划提示。"""
from __future__ import annotations

import json
from typing import Any

from .core import base_system_prompt


def selection_system_prompt(
    compact_tools: list[dict[str, Any]],
    *,
    no_tool_sentinel: str,
    routing_prompt: str | None = None,
) -> str:
    instruction = routing_prompt or (
        "当前任务是只读意图路由。只选择一个候选工具，不直接回答问题。"
        "当前问题是唯一意图来源，历史摘要仅用于解析明确的指代，不能替代当前问题。"
        f"普通问候、寒暄、缺少明确对象的模糊追问必须返回 {no_tool_sentinel}。"
        "除非当前问题明确要求系统概览、整体状态或系统简报，否则不得选择 workspace.briefing。"
        f"无法可靠匹配时 tool_name 必须为 {no_tool_sentinel}。"
        "arguments_json 必须是满足所选工具 parameters 的 JSON 对象字符串。"
    )
    return (
        base_system_prompt(include_date=True)
        + "\n"
        + instruction
        + "\n候选工具："
        + json.dumps(compact_tools, ensure_ascii=False, separators=(",", ":"))
    )


def read_plan_system_prompt(compact_tools: list[dict[str, Any]]) -> str:
    return (
        base_system_prompt(include_date=True)
        + "\n当前任务是只读诊断计划。只为用户明确要求的复合检查选择 2 到 4 个相互独立的工具。"
        "不得选择写入、下载、重试、清理或配置修改，不得重复工具，不得虚构参数。"
        "如果无法形成至少两个可靠步骤，返回不满足 schema 的空计划，由服务端放弃。"
        "arguments_json 必须是满足所选工具 parameters 的 JSON 对象字符串。\n候选工具："
        + json.dumps(compact_tools, ensure_ascii=False, separators=(",", ":"))
    )


def orchestration_route_instruction(*, no_tool_sentinel: str) -> str:
    return (
        "当前任务是 MediaFlux 业务工具路由。理解用户自然语言，只选择一个最能直接完成当前目标的候选工具，"
        "不要直接回答问题。候选中的 execute_read 可由服务端自动执行；prepare_confirmation 只会创建预览和"
        "行动计划，绝不会在本轮执行，禁止声称修改、刷新、下载、删除、同步或推送已经完成。"
        "当前消息是主要意图来源，最近上下文仅用于解析‘它、这部剧、刚才那个、刷新一下、重试’等明确且唯一的指代。"
        "若安全上下文显示已有待执行行动计划，取消、先别执行、放弃应选择取消计划能力；修改计划应生成唯一替代计划，系统会自动使旧计划失效。"
        "不得猜测订阅编号、任务编号、结果编号、媒体服务器实例、目录、资源站点列表或其他标识。"
        "如果缺少必填参数、对象不唯一、需要多个彼此独立的工具、只是寒暄，或候选能力不能完成目标，"
        f"tool_name 必须为 {no_tool_sentinel}。"
        "arguments_json 必须是严格满足所选工具 parameters 的 JSON 对象字符串。"
        "工具名属于内部实现，不得出现在面向用户的措辞中。"
    )


def confirmation_route_instruction(*, no_tool_sentinel: str) -> str:
    return (
        "当前任务是低风险受控设置规划。只选择一个候选工具并填写精确参数，不直接回答问题，"
        "更不能声称动作已经执行。服务端只会据此生成一项一次性行动计划；用户选择执行后才可能写入。"
        "只允许规划候选列表中明确存在的低风险开关或策略修改。刷新、同步、下载、推送、删除、"
        "清理和立即执行不属于本规划器。不得猜测订阅编号、任务名称、来源编号、实例编号、"
        "规则编号、站点清单或其他标识；可使用当前消息以及最近上下文里已经明确出现且唯一的对象。"
        "信息不完整、存在多个候选、只是查询状态、或参数无法严格满足 schema 时，"
        f"tool_name 必须为 {no_tool_sentinel}。"
        "历史只用于解析清晰指代，arguments_json 必须严格满足所选工具 parameters。"
    )
