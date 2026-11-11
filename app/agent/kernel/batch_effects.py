"""显式组合：一张冻结卡、一张票据，复用单操作的授权/执行/验证边界。

sync允许独立同步写；tracked只作为唯一前项，通过原任务绑定和受保护续行执行后项。
组合不是分布式事务；运行中失败保留已完成项，后续项不执行、不自动重放。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from inspect import signature
import secrets
import time
from copy import deepcopy
from dataclasses import replace

from app.agent.confirmation_contract import build_confirmation_contract
from app.agent.errors import ToolPipelineError
from app.agent.models import RiskLevel, ToolResult
from app.agent.public_safety import sanitize_public_text
from app.agent.public_view import public_result_state

from .batch_progress import batch_result
from .capabilities import KernelToolSpec, ToolEffect
from .tool_calls import PreparedEffect
from .invocation import invoke
from app.agent.schema import validate_json_schema
from .tool_calls import PipelineResult, ToolCallContext
from app.logger import get_logger

logger = get_logger(__name__)


def _fingerprint(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


async def _checkpoint(executor, context, index, result=None):
    from .completion_receipts import _completion_cipher, _late_receipt
    from .continuation import check_claim
    from .conversation_state import commit_conversation
    from .session_state import publication_matches
    from .state_types import StalePublicationError
    plan = context.active_effect_plan

    def update(state):
        if not publication_matches(context.lease, generation=state.generation, confirmed=state.metadata.get("confirmed_publication")):
            raise StalePublicationError("组合执行已失去原回合所有权")
        row = (state.metadata.get("effect_waits") or {}).get(plan.plan_id)
        if row is None or "batch_progress" not in row:
            raise StalePublicationError("组合缺少原确认的持久后台绑定")
        progress = row["batch_progress"]
        if len(progress["results"]) != index or progress["done"]:
            raise StalePublicationError("组合检查点已变化")
        if result is None:
            check_claim(state, context.lease, executor.state_store._clock())
            if state.metadata.get("stop_requested_generation") == context.lease.generation:
                return False
            if progress["executing"] is not None:
                raise StalePublicationError("前次写入结果待核验，不能重放")
            progress["executing"] = index
            return True
        if progress["executing"] != index and public_result_state(result) == "success":
            raise StalePublicationError("没有本项执行登记，不能记录成功")
        progress["results"].append({"tool": plan.arguments["steps"][index]["tool"], "result": deepcopy(result)})
        progress.update(executing=None, done=public_result_state(result) != "success" or index + 1 == len(plan.arguments["steps"]))
        final = batch_result(dict(plan.arguments), progress["results"], waiting=not progress["done"]).to_dict()
        seed = json.loads(_completion_cipher().decrypt(row["sealed"].encode()))
        row.update(final_result=final, receipt_message=_late_receipt(seed, final), foreground_final=True)
        commit_conversation(state, state.conversation)
        return True
    return await executor.state_store.update_effect_state(owner=context.owner, session_id=context.session_id, change=update)


async def resume_batch(executor, restored, context, progress):
    """只接收原 continuation 已验证的签名状态，复用相同组合执行函数。"""
    if not isinstance(progress, dict) or not progress.get("results"):
        raise ToolPipelineError("缺少组合前项终态，不能重新提交原操作", code="confirmation_invalid")
    plan = restored.effect_plan
    first = plan.arguments["steps"][0]
    child = replace(plan, plan_id=f"{plan.plan_id}_1", tool_name=first["tool"],
                    effect=executor.catalog.get(first["tool"]).for_arguments(first["arguments"]).effect,
                    arguments=first["arguments"], snapshot_fingerprint=first["snapshot"],
                    metadata=first["metadata"], confirmation_contract={"audit_contract": first["metadata"].get("confirmation", {})})
    from .completion_receipts import _seed_result
    executor._notify_effect("completed", plan=child, value=_seed_result({"result": progress["results"][0]["result"]}), elapsed_ms=0)
    value = await executor._run_effect(plan, context=replace(context, batch_progress=deepcopy(progress)))
    executor._notify_effect("completed", plan=plan, value=value, elapsed_ms=0)
    outcome = await executor._materialize_refs(executor.projector.project(value), context=context)
    await executor._commit_updates(context.lease, outcome.state_updates)
    return PipelineResult(restored.tool, dict(plan.arguments), replace(outcome, effect_plan=plan), effect_plan=plan)


def batch_tool(executor) -> KernelToolSpec:
    from app import sdk

    def selected(name: str) -> KernelToolSpec:
        try:
            tool = executor.catalog.get(name)
        except KeyError as exc:
            raise ToolPipelineError("冻结步骤对应能力已退役，请重新预检；不会重放旧操作", code="confirmation_stale") from exc
        if tool.effect is ToolEffect.READ or tool.metadata.get("batch_mode") not in {"sync", "tracked"}:
            raise ToolPipelineError("此方法不支持组合确认或组合能力已变化", code="invalid_arguments")
        return tool

    def normalize(arguments):
        steps = []
        for step in arguments["steps"]:
            method = step["method"]
            if method not in sdk.WRITE_METHODS or method == "session.batch":
                raise ToolPipelineError("组合步骤必须是支持组合的原生SDK写方法", code="invalid_arguments")
            tool = selected(method)
            bound = signature(sdk.METHODS[method]).bind(**step["arguments"])
            bound.apply_defaults()
            values = {key: value for key, value in bound.arguments.items() if value is not Ellipsis}
            validate_json_schema(values, tool.input_schema)
            steps.append({"tool": method, "arguments": tool.validator(values)})
        return {"steps": steps}

    def effect_for(arguments):
        return (ToolEffect.DANGER if any(selected(step["tool"]).for_arguments(step["arguments"]).effect is ToolEffect.DANGER
                                        for step in arguments["steps"]) else ToolEffect.WRITE)

    async def prepare(arguments: dict, context: ToolCallContext) -> PreparedEffect:
        steps = arguments["steps"]
        modes = [selected(step["tool"]).metadata.get("batch_mode") for step in steps]
        if "tracked" in modes and (modes[0] != "tracked" or modes.count("tracked") != 1):
            raise ToolPipelineError("后台操作只能作为唯一前项，后项必须是独立同步操作", code="invalid_arguments")
        if "tracked" in modes and (context.completion_scope is not None or context.continuation_binding is None):
            raise ToolPipelineError("当前入口未启用持久任务续行，不能预览异步组合", code="precondition_failed")
        frozen, previews = [], []
        for step in steps:
            tool = selected(step["tool"])
            result = await executor.prepare(tool.name, step["arguments"], context=context, prepare_only=True)
            prepared = result.prepared_effect
            if prepared is None:
                raise ToolPipelineError("组合中有无需变更的操作，请移除后重新预检", code="precondition_failed")
            frozen.append({"tool": tool.name, "arguments": dict(prepared.arguments or result.arguments),
                           "snapshot": prepared.snapshot_fingerprint, "metadata": dict(prepared.metadata),
                           "preview": dict(prepared.preview)})
            previews.append({"tool": tool.name, "result": dict(result.outcome.public_content)})
        effects = []
        for item in previews:
            result = item["result"]
            data = result.get("data") or {}
            changes = "；".join(item for item in data.get("effects", []) if isinstance(item, str))
            effects.append(sanitize_public_text(str(result.get("summary") or item["tool"]) + ("；" + changes if changes else ""), limit=600))
        preview = ToolResult(True, "confirmation_required", "确认后依次执行本卡列明的组合变更",
            data={"steps": previews, "effects": effects, "total": len(previews)})
        risk = (RiskLevel.DANGER if effect_for(arguments) is ToolEffect.DANGER else RiskLevel.LOW_WRITE if all(step["metadata"].get("risk") == "low_write" for step in frozen)
                else RiskLevel.WRITE)
        contract = build_confirmation_contract(tool_name="session.batch", risk=risk, preview=preview)
        contract.update(action="组合变更", object=f"本卡列明的 {len(frozen)} 项操作",
                        impact="全部快照复核后按顺序执行；失败时停止后续项。不授权任何新增操作。",
                        reversibility="不是整体事务；已完成项不会因后续失败自动回滚，请按各项回执核对。")
        arguments = {"steps": frozen}
        return PreparedEffect(preview=preview.to_dict(), arguments=arguments,
                              snapshot_fingerprint=_fingerprint(arguments),
                              metadata={"risk": risk.value, "confirmation": contract})

    async def execute(arguments: dict, expected: str, context: ToolCallContext) -> ToolResult:
        if not secrets.compare_digest(_fingerprint(arguments), expected) or context.active_effect_plan is None:
            raise ToolPipelineError("组合确认内容已变化", code="confirmation_invalid")
        frozen, parent = arguments["steps"], context.active_effect_plan
        tracked = selected(frozen[0]["tool"]).metadata.get("batch_mode") == "tracked"
        progress = context.batch_progress
        results = deepcopy(progress["results"]) if progress is not None else []
        start = len(results)
        if progress is not None and progress["done"]:
            return batch_result(arguments, results)
        if progress is not None and progress["executing"] is not None:
            unknown = {"ok": False, "status": "outcome_unknown", "summary": "前次已进入本项写入，结果待核验；不会自动重放"}
            await _checkpoint(executor, context, start, unknown)
            return batch_result(arguments, [*results, {"tool": frozen[start]["tool"], "result": unknown}])
        # 全单预复验只查引用、权限和同步快照；子项在_run_effect重新授权并领取一次执行额度。
        # tracked再次prepare会替换冻结计划，只由原执行器校验并受理。
        try:
            for step in frozen[start:]:
                tool = selected(step["tool"])
                resolved = await executor._authorize(tool, dict(step["arguments"]), context, confirmation=True)
                if tool.metadata.get("batch_mode") == "tracked":
                    continue
                assert tool.prepare is not None  # selected 仅允许已校验的写工具。
                observed = await invoke(tool.prepare, resolved, context)
                if not isinstance(observed, PreparedEffect) or observed.snapshot_fingerprint != step["snapshot"]:
                    raise ToolPipelineError("组合后项状态已变化，未执行新的写入；请重新预检", code="confirmation_stale")
        except ToolPipelineError as exc:
            if progress is None:
                raise
            refused = {"ok": False, "status": "not_started", "summary": str(exc)}
            await _checkpoint(executor, context, start, refused)
            return batch_result(arguments, [*results, {"tool": frozen[start]["tool"], "result": refused}])
        for index in range(start, len(frozen)):
            step, started = frozen[index], time.monotonic()
            tool = selected(step["tool"]).for_arguments(step["arguments"])
            if context.cancellation.cancelled or (results and public_result_state(results[-1]["result"]) != "success"):
                break
            if tracked and index > 0 and not await _checkpoint(executor, context, index):
                stopped = {"ok": False, "status": "not_started", "summary": "已停止，未执行剩余授权操作"}
                await _checkpoint(executor, context, index, stopped)
                results.append({"tool": tool.name, "result": stopped})
                break
            child = replace(parent, plan_id=f"{parent.plan_id}_{index + 1}", tool_name=tool.name, effect=tool.effect,
                            arguments=step["arguments"], snapshot_fingerprint=step["snapshot"],
                            preview=step["preview"], metadata=step["metadata"],
                            confirmation_contract={"audit_contract": step["metadata"].get("confirmation", {})})
            completion = None
            if tracked and index == 0:
                if context.completion_scope is None or not context.completion_scope.origin:
                    raise ToolPipelineError("缺少原确认的持久续行范围", code="confirmation_invalid")
                descriptor = {"tool": parent.tool_name, "snapshot": parent.snapshot_fingerprint, "effect": parent.effect.value,
                              "owner_generation": parent.owner_generation, "metadata": dict(parent.metadata),
                              "confirmation_contract": dict(parent.confirmation_contract)}
                completion = replace(context.completion_scope, origin={**context.completion_scope.origin, "batch_plan": descriptor})
            try:
                await asyncio.to_thread(executor.effect_lifecycle.substep_started, plan=child)
                value = await executor._run_effect(child, context=replace(context, completion_scope=completion))
            except ToolPipelineError as exc:
                executor._notify_effect("failed", plan=child, code=exc.code, elapsed_ms=int((time.monotonic() - started) * 1000))
                value = {"ok": False, "status": exc.code if exc.code in {"outcome_unknown", "result_unknown"} else "failed",
                         "summary": str(exc), "error": exc.code}
            except Exception as exc:
                logger.warning("批量写操作子项执行异常，结果未核验 type=%s", type(exc).__name__)
                executor._notify_effect("interrupted", plan=child)
                value = {"ok": False, "status": "outcome_unknown", "summary": "该项执行结果未核验，已停止后续操作；请先检查实际状态"}
            else:
                executor._notify_effect("completed", plan=child, value=value, elapsed_ms=int((time.monotonic() - started) * 1000))
            outcome = await executor._materialize_refs(executor.projector.project(value), context=context)
            await executor._commit_updates(context.lease, outcome.state_updates)
            public = dict(outcome.public_content)
            results.append({"tool": tool.name, "result": public})
            if tracked and index == 0:
                if public_result_state(public) in {"pending", "submitted"}:
                    return batch_result(arguments, results, waiting=True)
                if public_result_state(public) != "success":
                    break
            elif tracked:
                await _checkpoint(executor, context, index, public)
        return batch_result(arguments, results)

    return KernelToolSpec(
        name="session.batch", domain="agent", effect=ToolEffect.WRITE,
        description=sdk.session.batch.__doc__ or "组合确认",
        input_schema={"type": "object", "required": ["steps"], "additionalProperties": False, "properties": {
            "steps": {"type": "array", "minItems": 1, "items": {
                "type": "object", "required": ["method", "arguments"], "additionalProperties": False,
                "properties": {"method": {"type": "string"}, "arguments": {"type": "object"}}}}}},
        validator=normalize, prepare=prepare, execute_confirmed=execute, effect_resolver=effect_for,
        availability=lambda _: False,  # SDK effect端点；模型发现/直接调用不能准入。
        metadata={"source_kind": "system_state", "risk": "write", "kernel_utility": True},
    )
