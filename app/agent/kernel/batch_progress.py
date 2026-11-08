"""冻结组合的纯进度投影；观察终态不执行后续写入。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import Any

from app.agent.models import ToolResult
from app.agent.public_view import public_result_state

from .completion_receipts import EFFECT_PENDING


def restored_effect_tool(catalog, name, arguments):
    """仅供已签名持久计划/续行恢复；旧标识没有目录注册或新预检入口。"""
    tool = catalog.get("session.batch" if name == "agent.batch" else name).for_arguments(arguments)
    return replace(tool, name=name) if name == "agent.batch" else tool


def batch_result(arguments: dict, results: list[dict], *, waiting: bool = False) -> ToolResult:
    items = deepcopy(results)
    for step in arguments["steps"][len(items):]:
        items.append({"tool": step["tool"], "result": {"ok": False, "status": "not_started",
                      "summary": "等待已确认前项完成" if waiting else "未执行"}})
    done = sum(public_result_state(item["result"]) == "success" for item in items)
    data: dict[str, Any] = {"steps": items, "completed": done, "total": len(items)}
    if waiting:
        data["background_job"] = {"kind": "approved_batch", "status": "continuation_pending", "followup_pending": True}
    return ToolResult(waiting or done == len(items), "running" if waiting else "completed" if done == len(items) else "partial",
        f"组合变更：{done}/{len(items)} 项完成" + ("，剩余已确认操作将自动接续" if waiting else "") + "\n" + "\n".join(
            f"{i}. {item['result'].get('summary', '请核对执行结果')}" for i, item in enumerate(items, 1)), data=data)


def observe_batch(row: dict, origin: dict, result: dict) -> dict:
    """只记后台前项的真实终态；轮询器仍不执行任何后续写操作。"""
    if not origin.get("batch_plan"):
        return result
    progress = row.setdefault("batch_progress", {"results": [{"tool": origin["arguments"]["steps"][0]["tool"], "result": result}],
                                               "executing": None, "done": public_result_state(result) != "success"})
    active = (row.get("continuation") or {}).get("state") in EFFECT_PENDING
    if not active and progress["executing"] is not None:
        step = origin["arguments"]["steps"][progress["executing"]]
        progress["results"].append({"tool": step["tool"], "result": {
            "ok": False, "status": "outcome_unknown", "summary": "本项已开始写入，但回执未能保存；请核对实际状态，不会自动重放"}})
        progress.update(executing=None, done=True)
    return batch_result(origin["arguments"], progress["results"], waiting=not progress["done"] and active).to_dict()
